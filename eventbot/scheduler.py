from __future__ import annotations

import logging
from datetime import datetime, UTC

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent import persist_recommendations, promote_shared_events, run_agent
from .models import Run, User
from .prefs import UserPrefs, load_all_prefs, HOUSEHOLD_SLUG
from .settings import Settings
from .sources import recommend_source_events_for_user, update_source_events

logger = logging.getLogger(__name__)


def _cron_trigger(prefs: UserPrefs) -> CronTrigger:
    s = prefs.schedule
    if s.frequency == "daily":
        return CronTrigger(hour=s.hour, minute=0)
    if s.frequency == "monthly":
        return CronTrigger(day=s.day_of_month, hour=s.hour, minute=0)
    # default: weekly
    return CronTrigger(day_of_week=s.day_of_week[:3].lower(), hour=s.hour, minute=0)


async def _ensure_user(slug: str, display_name: str, session: AsyncSession) -> User:
    user = await session.scalar(select(User).where(User.slug == slug))
    if not user:
        user = User(slug=slug, display_name=display_name)
        session.add(user)
        await session.flush()
    return user


async def run_for_user(
    prefs: UserPrefs,
    session_factory: async_sessionmaker,
    settings: Settings,
    all_prefs: dict[str, UserPrefs] | None = None,
) -> None:
    logger.info("Starting run for %s", prefs.slug)
    async with session_factory() as session:
        async with session.begin():
            user = await _ensure_user(prefs.slug, prefs.display_name, session)
            run = Run(
                user_id=user.id,
                is_household=prefs.is_household,
                started_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()

            try:
                # 1. Pull in source-curated events from .ics feeds
                await update_source_events(session, settings)

                # 2. Agent-driven discovery (skipped if API keys are not set)
                other_prefs = (
                    [p for p in all_prefs.values() if not p.is_household]
                    if prefs.is_household and all_prefs
                    else None
                )
                candidates = await run_agent(
                    prefs=prefs,
                    user=user,
                    run=run,
                    session=session,
                    settings=settings,
                    all_user_prefs=other_prefs,
                )
                events = await persist_recommendations(
                    candidates=candidates,
                    user=user,
                    run=run,
                    session=session,
                    is_household=prefs.is_household,
                )

                # 3. Score source events for this user (unless this is the household run)
                source_recs: list = []
                if not prefs.is_household:
                    source_recs = await recommend_source_events_for_user(
                        user=user,
                        run=run,
                        session=session,
                        settings=settings,
                    )
                    await promote_shared_events(session)

                run.finished_at = datetime.now(UTC)
                run.event_count = len(events) + len(source_recs)

            except Exception as exc:
                logger.exception("Run failed for %s", prefs.slug)
                run.error = str(exc)
                run.finished_at = datetime.now(UTC)

    # Send digest after session commits
    if run.error is None:
        await _send_digest(prefs, settings, session_factory)

    logger.info("Finished run for %s — %d events", prefs.slug, run.event_count)


async def _send_digest(
    prefs: UserPrefs,
    settings: Settings,
    session_factory: async_sessionmaker,
) -> None:
    from .email import send_digest  # avoid circular at module load time
    try:
        await send_digest(prefs=prefs, settings=settings, session_factory=session_factory)
    except Exception:
        logger.exception("Failed to send digest for %s", prefs.slug)


def build_scheduler(
    session_factory: async_sessionmaker,
    settings: Settings,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    all_prefs = load_all_prefs(settings.preferences_dir)

    for slug, prefs in all_prefs.items():
        trigger = _cron_trigger(prefs)
        scheduler.add_job(
            run_for_user,
            trigger=trigger,
            id=f"run_{slug}",
            kwargs={
                "prefs": prefs,
                "session_factory": session_factory,
                "settings": settings,
                "all_prefs": all_prefs if slug == HOUSEHOLD_SLUG else None,
            },
            replace_existing=True,
        )
        logger.info(
            "Scheduled %s (%s) — %s",
            prefs.display_name,
            slug,
            prefs.schedule.frequency,
        )

    return scheduler


def reload_scheduler(
    scheduler: AsyncIOScheduler,
    session_factory: async_sessionmaker,
    settings: Settings,
) -> None:
    """Remove all user jobs and re-add them from the current YAML files."""
    for job in scheduler.get_jobs():
        if job.id.startswith("run_"):
            job.remove()

    all_prefs = load_all_prefs(settings.preferences_dir)
    for slug, prefs in all_prefs.items():
        trigger = _cron_trigger(prefs)
        scheduler.add_job(
            run_for_user,
            trigger=trigger,
            id=f"run_{slug}",
            kwargs={
                "prefs": prefs,
                "session_factory": session_factory,
                "settings": settings,
                "all_prefs": all_prefs if slug == HOUSEHOLD_SLUG else None,
            },
            replace_existing=True,
        )
