"""One-off backfill: merge pre-existing duplicate events already in the DB.

Deduplication normally happens at persist time, so rows written before that
feature shipped are left untouched. This script clusters existing events with
the same fuzzy title+venue+date logic and folds each cluster into a single
canonical row (the lowest id), widening its date span and re-homing the
duplicates' recommendations and feedback.

Usage (inside the app environment / container, with DATA_DIR pointing at the
data dir that holds eventbot.db):

    python -m eventbot.backfill_dedup            # dry run: report only
    python -m eventbot.backfill_dedup --apply    # actually merge + delete

Recommendations/feedback are unique per (event, user); when a duplicate and the
canonical both have a row for the same user, the richer one is kept (best score
for recommendations, most recent rating for feedback) and the other removed.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from . import dedup
from .calendar_util import DEFAULT_TZ, parse_date_to_datetime
from .models import Event, Feedback, Recommendation
from .settings import get_settings

logger = logging.getLogger(__name__)


def _bounds(event: Event, tz: str):
    start = parse_date_to_datetime(event.event_date, tz)
    end = parse_date_to_datetime(event.end_date, tz) if event.end_date else None
    return start, end


async def _merge_recommendations(session: AsyncSession, canonical: Event, dup: Event) -> None:
    dup_recs = list(await session.scalars(
        select(Recommendation).where(Recommendation.event_id == dup.id)
    ))
    for r in dup_recs:
        existing = await session.scalar(
            select(Recommendation).where(
                Recommendation.event_id == canonical.id,
                Recommendation.user_id == r.user_id,
            )
        )
        if existing:
            if r.score > existing.score:
                existing.score = r.score
                existing.relevance_notes = r.relevance_notes
                existing.run_id = r.run_id
            existing.is_household = existing.is_household or r.is_household
            await session.delete(r)
        else:
            r.event_id = canonical.id
        await session.flush()


async def _merge_feedback(session: AsyncSession, canonical: Event, dup: Event) -> None:
    dup_fb = list(await session.scalars(
        select(Feedback).where(Feedback.event_id == dup.id)
    ))
    for f in dup_fb:
        existing = await session.scalar(
            select(Feedback).where(
                Feedback.event_id == canonical.id,
                Feedback.user_id == f.user_id,
            )
        )
        if existing:
            # Keep the most recent rating.
            if f.rated_at and (existing.rated_at is None or f.rated_at > existing.rated_at):
                existing.rating = f.rating
                existing.rated_at = f.rated_at
            await session.delete(f)
        else:
            f.event_id = canonical.id
        await session.flush()


async def _merge_into_canonical(
    session: AsyncSession, canonical: Event, dup: Event, tz: str
) -> None:
    cs, ce = _bounds(canonical, tz)
    ds, de = _bounds(dup, tz)
    new_start, new_end = dedup.merge_span(cs, ce, ds, de)
    new_url = dedup.prefer_richer(canonical.url, dup.url)
    new_desc = dedup.prefer_richer(canonical.description, dup.description)

    # Re-home associations and remove the duplicate FIRST, so the duplicate's
    # (venue, event_date, title_slug) identity is freed before we widen the
    # canonical onto that date — otherwise the unique constraint trips.
    await _merge_recommendations(session, canonical, dup)
    await _merge_feedback(session, canonical, dup)
    await session.delete(dup)
    await session.flush()

    if new_start is not None:
        canonical.event_date = new_start.date().isoformat()
        canonical.start_at = new_start
    if new_end is not None:
        canonical.end_date = new_end.date().isoformat()
        canonical.end_at = new_end
    canonical.timezone = canonical.timezone or tz
    canonical.url = new_url or canonical.url or ""
    canonical.description = new_desc
    await session.flush()


async def _absorb(session: AsyncSession, canonical: Event, dup: Event) -> None:
    """Move a duplicate's recommendations/feedback onto the canonical and delete
    it, without touching the canonical's dates (used for recurring collapse)."""
    await _merge_recommendations(session, canonical, dup)
    await _merge_feedback(session, canonical, dup)
    await session.delete(dup)
    await session.flush()


async def _collapse_recurring_masters(
    session: AsyncSession, apply: bool
) -> tuple[int, list[dict]]:
    """Collapse duplicate recurring series that share (venue, title_slug, rrule)."""
    from .eventmatch import time_key

    recs = list(await session.scalars(
        select(Event).where(Event.is_recurring.is_(True)).order_by(Event.id)
    ))
    groups: dict[tuple, list[Event]] = {}
    for e in recs:
        groups.setdefault((e.venue, e.title_slug, e.rrule or ""), []).append(e)

    # Split a (venue, title, rrule) group only when it holds 2+ distinct *real*
    # start times (e.g. a 9:30am and a 5:30pm session) — midnight/all-day rows
    # are a wildcard and stay with the single real-time subgroup.
    subgroups: list[list[Event]] = []
    for members in groups.values():
        real_times = {time_key(m.start_at) for m in members} - {None}
        if len(real_times) <= 1:
            subgroups.append(members)
        else:
            by_time: dict[str | None, list[Event]] = {}
            for m in members:
                by_time.setdefault(time_key(m.start_at), []).append(m)
            subgroups.extend(by_time.values())

    removed = 0
    plan: list[dict] = []
    for members in subgroups:
        if len(members) < 2:
            continue
        members.sort(key=lambda e: (e.event_date, e.id))  # earliest start = canonical
        canonical, dups = members[0], members[1:]
        plan.append({
            "canonical": (canonical.id, canonical.title, canonical.event_date),
            "duplicates": [(d.id, d.title, d.event_date) for d in dups],
        })
        removed += len(dups)
        if apply:
            for dup in dups:
                await _absorb(session, canonical, dup)
    return removed, plan


async def _suppress_covered_singletons(
    session: AsyncSession, tz: str, apply: bool
) -> tuple[int, list[dict]]:
    """Fold standalone occurrences into a recurring series that already covers
    their date (same fuzzy title+venue)."""
    from .calendar_util import expand_occurrences

    series = list(await session.scalars(select(Event).where(Event.is_recurring.is_(True))))
    if not series:
        return 0, []
    singles = list(await session.scalars(select(Event).where(Event.is_recurring.is_(False))))

    removed = 0
    plan: list[dict] = []
    for s in singles:
        sstart = parse_date_to_datetime(s.event_date, s.timezone or tz)
        if sstart is None:
            continue
        day_start = sstart.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1) - timedelta(seconds=1)  # stay within the day
        for ev in series:
            if ev.id == s.id:
                continue
            if dedup.variant_markers(s.title) != dedup.variant_markers(ev.title):
                continue
            if dedup.title_similarity(s.title, ev.title, s.venue, ev.venue) < dedup.TITLE_THRESHOLD:
                continue
            if dedup.venue_similarity(s.venue, ev.venue) < dedup.VENUE_THRESHOLD:
                continue
            if expand_occurrences(ev, day_start, day_end):
                plan.append({
                    "canonical": (ev.id, ev.title, "series"),
                    "duplicates": [(s.id, s.title, s.event_date)],
                })
                removed += 1
                if apply:
                    await _absorb(session, ev, s)
                break
    return removed, plan


async def backfill(session: AsyncSession, tz: str = DEFAULT_TZ, apply: bool = False) -> dict:
    """Cluster and merge duplicate events. Returns a summary dict."""
    # 1. Collapse duplicate recurring masters, then drop standalone occurrences a
    #    series already covers (must run before span clustering reads the rows).
    recurring_removed, recurring_plan = await _collapse_recurring_masters(session, apply)
    covered_removed, covered_plan = await _suppress_covered_singletons(session, tz, apply)

    # 2. Span/fuzzy clustering for the remaining one-off events.
    events = list(await session.scalars(select(Event).order_by(Event.id)))
    items = [(e.title, e.venue, *_bounds(e, tz)) for e in events]
    clusters = dedup.cluster_duplicates(items)

    merged_events = 0
    removed_events = 0
    plan: list[dict] = recurring_plan + covered_plan

    for cluster in clusters:
        if len(cluster) < 2:
            continue
        members = sorted((events[i] for i in cluster), key=lambda e: e.id)
        canonical, dups = members[0], members[1:]
        plan.append({
            "canonical": (canonical.id, canonical.title, canonical.event_date),
            "duplicates": [(d.id, d.title, d.event_date) for d in dups],
        })
        merged_events += 1
        removed_events += len(dups)
        if apply:
            for dup in dups:
                await _merge_into_canonical(session, canonical, dup, tz)

    if apply:
        # Recompute household flags now that recommendations moved around.
        from .agent import promote_shared_events
        await promote_shared_events(session)
        await session.commit()

    return {
        "total_events": len(events) + recurring_removed + covered_removed,
        "clusters_with_duplicates": merged_events,
        "events_removed": removed_events + recurring_removed + covered_removed,
        "recurring_masters_collapsed": recurring_removed,
        "covered_singletons_removed": covered_removed,
        "plan": plan,
    }


def _print_summary(summary: dict, apply: bool) -> None:
    verb = "Merged" if apply else "Would merge"
    print(f"Scanned {summary['total_events']} events.")
    print(f"{summary['clusters_with_duplicates']} duplicate cluster(s); "
          f"{verb.lower()} {summary['events_removed']} row(s) into canonicals.")
    print(f"  recurring masters collapsed: {summary.get('recurring_masters_collapsed', 0)}")
    print(f"  standalone occurrences folded into a series: "
          f"{summary.get('covered_singletons_removed', 0)}")
    for entry in summary["plan"]:
        cid, ctitle, cdate = entry["canonical"]
        print(f"\n  keep  [{cid}] {ctitle!r} ({cdate})")
        for did, dtitle, ddate in entry["duplicates"]:
            print(f"  merge [{did}] {dtitle!r} ({ddate})")
    if not apply and summary["events_removed"]:
        print("\nDry run only. Re-run with --apply to perform the merge.")


async def _amain(apply: bool) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Ensure newer columns (e.g. end_date) exist so the script runs against
        # databases created before those columns shipped.
        from .main import _apply_column_migrations
        async with engine.begin() as conn:
            await conn.run_sync(_apply_column_migrations)

        async with factory() as session:
            summary = await backfill(session, apply=apply)
        _print_summary(summary, apply)
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Merge duplicate events already in the DB.")
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually merge and delete duplicates (default is a dry run).",
    )
    args = parser.parse_args()
    asyncio.run(_amain(args.apply))


if __name__ == "__main__":
    main()
