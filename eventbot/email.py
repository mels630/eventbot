from __future__ import annotations

import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import aiosmtplib
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .models import Event, Feedback, Recommendation, Run, User
from .prefs import UserPrefs, HOUSEHOLD_SLUG, load_all_prefs
from .settings import Settings

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _jinja_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)


async def _load_recent_events(
    user: User,
    session_factory: async_sessionmaker,
    is_household: bool = False,
    limit: int = 20,
) -> list[dict]:
    async with session_factory() as session:
        rows = await session.execute(
            select(Event, Recommendation)
            .join(Recommendation, Recommendation.event_id == Event.id)
            .where(
                Recommendation.user_id == user.id,
                Recommendation.is_household == is_household,
            )
            .order_by(Recommendation.score.desc())
            .limit(limit)
        )
        results = []
        for event, rec in rows:
            fb = await session.scalar(
                select(Feedback).where(
                    Feedback.event_id == event.id,
                    Feedback.user_id == user.id,
                )
            )
            results.append({
                "id": event.id,
                "title": event.title,
                "venue": event.venue,
                "event_date": event.event_date,
                "url": event.url,
                "description": event.description,
                "score": rec.score,
                "relevance_notes": rec.relevance_notes,
                "feedback": fb.rating if fb else None,
            })
        return results


async def _load_household_events(
    session_factory: async_sessionmaker,
    limit: int = 10,
) -> list[dict]:
    async with session_factory() as session:
        # Events recommended to 2+ users, most recent run
        from sqlalchemy import func
        shared_ids = (
            select(Recommendation.event_id)
            .group_by(Recommendation.event_id)
            .having(func.count(Recommendation.user_id.distinct()) >= 2)
        ).scalar_subquery()

        rows = await session.execute(
            select(Event, Recommendation)
            .join(Recommendation, Recommendation.event_id == Event.id)
            .where(Event.id.in_(shared_ids))
            .order_by(Recommendation.score.desc())
            .distinct(Event.id)
            .limit(limit)
        )

        results = []
        for event, rec in rows:
            # Find which user slugs were recommended this event
            user_recs = await session.execute(
                select(User.display_name)
                .join(Recommendation, Recommendation.user_id == User.id)
                .where(Recommendation.event_id == event.id, User.slug != HOUSEHOLD_SLUG)
                .distinct()
            )
            who = [r[0] for r in user_recs]
            results.append({
                "id": event.id,
                "title": event.title,
                "venue": event.venue,
                "event_date": event.event_date,
                "url": event.url,
                "description": event.description,
                "score": rec.score,
                "who": who,
            })
        return results


async def send_digest(
    prefs: UserPrefs,
    settings: Settings,
    session_factory: async_sessionmaker,
) -> None:
    if not settings.smtp_enabled:
        logger.info("SMTP not configured; skipping digest for %s", prefs.slug)
        return

    env = _jinja_env()

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.slug == prefs.slug))
        if not user:
            logger.warning("No DB user found for %s, skipping digest", prefs.slug)
            return

    if prefs.is_household:
        events = await _load_recent_events(user, session_factory, is_household=True)
        household_events: list[dict] = []
        template = env.get_template("email_household.html")
        subject = "Household Event Digest"
        html = template.render(prefs=prefs, events=events)
        recipients = _get_all_emails(settings)
    else:
        events = await _load_recent_events(user, session_factory, is_household=False)
        household_events = await _load_household_events(session_factory)
        template = env.get_template("email_personal.html")
        subject = f"Your Event Digest, {prefs.display_name}"
        html = template.render(
            prefs=prefs,
            events=events,
            household_events=household_events,
            base_url=f"http://localhost:8080",
        )
        recipients = [prefs.email]

    await _smtp_send(
        html=html,
        subject=subject,
        recipients=recipients,
        settings=settings,
    )
    logger.info("Digest sent to %s", recipients)


def _get_all_emails(settings: Settings) -> list[str]:
    try:
        all_prefs = load_all_prefs(settings.preferences_dir)
        return [
            p.email
            for p in all_prefs.values()
            if p.email and not p.is_household
        ]
    except Exception:
        return []


async def _smtp_send(
    html: str,
    subject: str,
    recipients: list[str],
    settings: Settings,
) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))

    await aiosmtplib.send(
        msg,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        start_tls=True,
    )
