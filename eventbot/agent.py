from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, UTC
from typing import Any

logger = logging.getLogger(__name__)

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Event, Feedback, Recommendation, Run, SearchCache, User
from .prefs import UserPrefs
from .settings import Settings

CACHE_TTL_HOURS = 6
MAX_RECOMMENDATIONS = 10
HOUSEHOLD_SCORE_THRESHOLD = 0.6  # min score for an event to count toward a user in household mode


# --------------------------------------------------------------------------- #
# Structured types returned by the agent                                       #
# --------------------------------------------------------------------------- #

class EventCandidate:
    def __init__(
        self,
        title: str,
        venue: str,
        event_date: str,
        url: str,
        description: str,
        score: float,
        relevance_notes: str,
        end_date: str = "",
    ) -> None:
        self.title = title
        self.venue = venue
        self.event_date = event_date
        self.end_date = end_date
        self.url = url
        self.description = description
        self.score = score
        self.relevance_notes = relevance_notes

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EventCandidate":
        return cls(
            title=d.get("title", ""),
            venue=d.get("venue", "Unknown venue"),
            event_date=d.get("event_date", ""),
            end_date=d.get("end_date", "") or "",
            url=d.get("url", ""),
            description=d.get("description", ""),
            score=float(d.get("score", 0.5)),
            relevance_notes=d.get("relevance_notes", ""),
        )


# --------------------------------------------------------------------------- #
# Search cache helpers                                                         #
# --------------------------------------------------------------------------- #

def _query_hash(query: str) -> str:
    return hashlib.sha256(query.lower().strip().encode()).hexdigest()[:16]


async def _cached_search(
    query: str,
    tavily_client: Any,
    session: AsyncSession,
) -> list[dict]:
    qhash = _query_hash(query)
    cutoff = datetime.now(UTC) - timedelta(hours=CACHE_TTL_HOURS)

    row = await session.scalar(
        select(SearchCache).where(
            SearchCache.query_hash == qhash,
            SearchCache.cached_at >= cutoff,
        )
    )
    if row:
        return json.loads(row.results_json)

    results = tavily_client.search(query, max_results=8)
    raw = results.get("results", [])

    existing = await session.scalar(
        select(SearchCache).where(SearchCache.query_hash == qhash)
    )
    if existing:
        existing.results_json = json.dumps(raw)
        existing.cached_at = datetime.now(UTC)
    else:
        session.add(SearchCache(
            query_hash=qhash,
            query=query,
            results_json=json.dumps(raw),
        ))
    await session.flush()
    return raw


# --------------------------------------------------------------------------- #
# Feedback history summary                                                     #
# --------------------------------------------------------------------------- #

async def _feedback_summary(user: User, session: AsyncSession) -> str:
    rows = await session.execute(
        select(Feedback, Event)
        .join(Event, Feedback.event_id == Event.id)
        .where(Feedback.user_id == user.id)
        .order_by(Feedback.rated_at.desc())
        .limit(40)
    )
    liked, disliked = [], []
    for fb, ev in rows:
        (liked if fb.rating > 0 else disliked).append(ev.title)

    parts = []
    if liked:
        parts.append(f"Previously liked: {', '.join(liked[:20])}")
    if disliked:
        parts.append(f"Previously disliked: {', '.join(disliked[:20])}")
    return "; ".join(parts) if parts else "No feedback history yet."


# --------------------------------------------------------------------------- #
# Core agent run                                                               #
# --------------------------------------------------------------------------- #

async def run_agent(
    prefs: UserPrefs,
    user: User,
    run: Run,
    session: AsyncSession,
    settings: Settings,
    all_user_prefs: list[UserPrefs] | None = None,  # required for household mode
) -> list[EventCandidate]:
    """Run the event-discovery agent for one user or the household."""
    if not settings.agent_enabled:
        logger.warning("Agent disabled: ANTHROPIC_API_KEY and/or TAVILY_API_KEY not set")
        return []

    from tavily import TavilyClient  # lazy import to avoid startup cost

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    tavily = TavilyClient(api_key=settings.tavily_api_key)

    today = datetime.now(UTC).date()
    window_end = today + timedelta(days=30)
    feedback_ctx = await _feedback_summary(user, session)

    household_ctx = ""
    if prefs.is_household and all_user_prefs:
        names = [p.display_name for p in all_user_prefs if not p.is_household]
        household_ctx = (
            f"\nYou are searching for events that multiple household members could enjoy together. "
            f"Household members: {', '.join(names)}. "
            f"Score an event highly if it appeals to 2 or more members based on their shared interests."
        )

    system_prompt = f"""You are an event discovery assistant for {prefs.display_name}.
Location: {prefs.location}
Timezone: {prefs.timezone}
Interests: {', '.join(prefs.interests) if prefs.interests else 'general events'}
Blocklist (never recommend): {', '.join(prefs.blocklist) if prefs.blocklist else 'nothing blocked'}
{feedback_ctx}
{household_ctx}

Today is {today}. Search for events happening between {today} and {window_end}.

Your job:
1. Generate 6-10 targeted web search queries covering different facets of the interests and date range.
2. For each query, call the web_search tool.
3. Extract real upcoming events from the results — ignore articles, reviews, or past events.
4. Deduplicate aggressively. Treat listings as the SAME event — and return them only once —
   when they share a venue and date even if the titles differ in wording, order, or extra
   decoration (e.g. "Jazz Night at Blue Moon" vs "Blue Moon Presents: Jazz Night — Tickets").
5. For an event that runs over multiple days or is ongoing (festivals, exhibits, runs), return
   it ONCE with event_date = the start date and end_date = the end date. Do NOT emit one entry
   per day.
6. Score and rank the top {MAX_RECOMMENDATIONS} events by relevance to the interests listed above.
7. Return your final ranked list as a JSON array using the finish_with_events tool.

Each event object must have:
  title, venue, event_date (YYYY-MM-DD or best approximation), url, description (1-2 sentences), score (0.0-1.0), relevance_notes (why this fits)
Optionally include end_date (YYYY-MM-DD) for multiday or ongoing events.
"""

    tools = [
        {
            "name": "web_search",
            "description": "Search the web for upcoming events.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string"}
                },
                "required": ["query"],
            },
        },
        {
            "name": "finish_with_events",
            "description": "Return the final ranked list of event recommendations.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "events": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "venue": {"type": "string"},
                                "event_date": {"type": "string"},
                                "end_date": {
                                    "type": "string",
                                    "description": "End date (YYYY-MM-DD) for multiday/ongoing events; omit for single-day events.",
                                },
                                "url": {"type": "string"},
                                "description": {"type": "string"},
                                "score": {"type": "number"},
                                "relevance_notes": {"type": "string"},
                            },
                            "required": ["title", "venue", "event_date", "url", "score"],
                        },
                    }
                },
                "required": ["events"],
            },
        },
    ]

    messages: list[dict] = [{"role": "user", "content": "Find upcoming events for me."}]
    candidates: list[EventCandidate] = []

    for _ in range(20):  # max iterations
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break

        tool_results = []
        done = False

        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "finish_with_events":
                raw_events = block.input.get("events", [])
                candidates = [EventCandidate.from_dict(e) for e in raw_events]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Done.",
                })
                done = True

            elif block.name == "web_search":
                query = block.input.get("query", "")
                results = await _cached_search(query, tavily, session)
                formatted = "\n\n".join(
                    f"Title: {r.get('title', '')}\nURL: {r.get('url', '')}\nSnippet: {r.get('content', '')}"
                    for r in results
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": formatted or "No results found.",
                })

        messages.append({"role": "user", "content": tool_results})

        if done:
            break

    return candidates


# --------------------------------------------------------------------------- #
# Persist results to DB                                                        #
# --------------------------------------------------------------------------- #

def _title_slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]


def _event_bounds(event: Event, tz: str) -> tuple[Any, Any]:
    """(start, end) datetimes for an existing event, parsed from its date strings."""
    from .calendar_util import parse_date_to_datetime

    start = parse_date_to_datetime(event.event_date, tz)
    end = parse_date_to_datetime(event.end_date, tz) if event.end_date else None
    return start, end


async def _find_matching_event(
    c: EventCandidate,
    start_at: Any,
    end_at: Any,
    seen: list[Event],
    session: AsyncSession,
    tz: str,
) -> Event | None:
    """Locate an existing event that is the same as candidate ``c`` using fuzzy
    title+venue+date matching. Checks this run's events first, then the DB."""
    from . import dedup

    def matches(ev: Event) -> bool:
        es, ee = _event_bounds(ev, tz)
        return dedup.is_same_event(
            c.title, c.venue, start_at, end_at,
            ev.title, ev.venue, es, ee,
        )

    # 1. Events already touched during this run (handles batch duplicates and
    #    avoids depending on a flush being visible to the query below).
    for ev in seen:
        if matches(ev):
            return ev

    seen_ids = {ev.id for ev in seen if ev.id is not None}

    if start_at is None:
        # Unparseable date: fall back to exact identity match.
        ev = await session.scalar(
            select(Event).where(
                Event.venue == c.venue,
                Event.event_date == c.event_date,
                Event.title_slug == _title_slug(c.title),
            )
        )
        return ev if ev and ev.id not in seen_ids else None

    # 2. DB events whose stored span overlaps the candidate's.
    from .eventmatch import find_db_match

    return await find_db_match(
        session, c.title, c.venue, start_at, end_at, tz, exclude_ids=seen_ids
    )


def _merge_into_event(
    event: Event, c: EventCandidate, start_at: Any, end_at: Any, tz: str
) -> None:
    """Widen an existing event's span and backfill richer text from candidate ``c``."""
    from . import dedup

    es, ee = _event_bounds(event, tz)
    new_start, new_end = dedup.merge_span(es, ee, start_at, end_at)
    if new_start is not None:
        event.event_date = new_start.date().isoformat()
        event.start_at = new_start
    event.end_date = new_end.date().isoformat() if new_end is not None else None
    event.end_at = new_end
    event.timezone = event.timezone or tz
    event.is_all_day = True
    event.url = dedup.prefer_richer(event.url, c.url) or event.url or ""
    event.description = dedup.prefer_richer(event.description, c.description)


async def persist_recommendations(
    candidates: list[EventCandidate],
    user: User,
    run: Run,
    session: AsyncSession,
    is_household: bool = False,
    default_tz: str = "America/Los_Angeles",
) -> list[Event]:
    from .calendar_util import parse_date_to_datetime

    saved: list[Event] = []
    seen: list[Event] = []  # events touched during this call (for batch dedup)

    for c in candidates:
        # Agent candidates carry date strings; derive all-day start/end so they
        # flow into the .ics export and calendar views.
        start_at = parse_date_to_datetime(c.event_date, default_tz)
        end_at = parse_date_to_datetime(c.end_date, default_tz) if c.end_date else None

        event = await _find_matching_event(c, start_at, end_at, seen, session, default_tz)

        if event is None:
            event = Event(
                title=c.title,
                title_slug=_title_slug(c.title),
                venue=c.venue,
                event_date=c.event_date,
                end_date=(end_at.date().isoformat() if end_at is not None else None),
                url=c.url,
                description=c.description,
                start_at=start_at,
                end_at=end_at,
                timezone=default_tz,
                is_all_day=True,
            )
            session.add(event)
            await session.flush()
        else:
            _merge_into_event(event, c, start_at, end_at, default_tz)
            await session.flush()

        if event not in seen:
            seen.append(event)

        existing_rec = await session.scalar(
            select(Recommendation).where(
                Recommendation.event_id == event.id,
                Recommendation.user_id == user.id,
            )
        )
        if existing_rec:
            existing_rec.run_id = run.id
            # When several candidates collapse onto one event, keep the best score.
            if c.score >= existing_rec.score:
                existing_rec.score = c.score
                existing_rec.relevance_notes = c.relevance_notes
            existing_rec.is_household = is_household
        else:
            session.add(Recommendation(
                event_id=event.id,
                user_id=user.id,
                run_id=run.id,
                score=c.score,
                relevance_notes=c.relevance_notes,
                is_household=is_household,
            ))
        await session.flush()

        if event not in saved:
            saved.append(event)

    await session.flush()
    return saved


async def promote_shared_events(session: AsyncSession) -> None:
    """Mark recommendations as household if the same event was recommended to 2+ users."""
    from sqlalchemy import func, and_

    shared_event_ids = (
        select(Recommendation.event_id)
        .where(Recommendation.is_household.is_(False))
        .group_by(Recommendation.event_id)
        .having(func.count(Recommendation.user_id.distinct()) >= 2)
    ).scalar_subquery()

    recs = await session.scalars(
        select(Recommendation).where(
            and_(
                Recommendation.event_id.in_(shared_event_ids),
                Recommendation.is_household.is_(False),
            )
        )
    )
    for rec in recs:
        rec.is_household = True
    await session.flush()
