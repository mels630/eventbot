from __future__ import annotations

import calendar as _calendar
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, UTC
from pathlib import Path

import pytz

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .models import Base, Event, Feedback, Recommendation, Run, User
from .prefs import (
    HOUSEHOLD_SLUG,
    UserPrefs,
    load_all_prefs,
    load_prefs,
    save_prefs,
    synthesize_household,
)
from .ical_export import generate_ical_for_user
from .calendar_util import (
    DEFAULT_TZ,
    format_when,
    google_calendar_url,
    is_upcoming,
    occurrence_entries,
)
from .scheduler import build_scheduler, reload_scheduler, run_for_user
from .settings import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.preferences_dir.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(settings.db_url, echo=False)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    scheduler = build_scheduler(SessionFactory, settings)
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(title="eventbot", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

async def _get_user_or_404(slug: str) -> tuple[UserPrefs, User]:
    all_prefs = load_all_prefs(settings.preferences_dir)
    if slug not in all_prefs:
        raise HTTPException(status_code=404, detail=f"User '{slug}' not found")
    prefs = all_prefs[slug]
    async with SessionFactory() as session:
        user = await session.scalar(select(User).where(User.slug == slug))
    return prefs, user


def _safe_tz(tz_name: str | None):
    try:
        return pytz.timezone(tz_name or DEFAULT_TZ)
    except Exception:
        return pytz.timezone(DEFAULT_TZ)


async def _events_for_user(
    user_id: int,
    is_household: bool = False,
    limit: int = 20,
    display_tz: str = DEFAULT_TZ,
) -> list[dict]:
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        rows = await session.execute(
            select(Event, Recommendation)
            .join(Recommendation, Recommendation.event_id == Event.id)
            .where(
                Recommendation.user_id == user_id,
                Recommendation.is_household == is_household,
            )
            .order_by(Recommendation.score.desc())
        )
        result = []
        for event, rec in rows:
            if not is_upcoming(event, now):
                continue
            fb = await session.scalar(
                select(Feedback).where(
                    Feedback.event_id == event.id,
                    Feedback.user_id == user_id,
                )
            )
            result.append({
                "id": event.id,
                "title": event.title,
                "venue": event.venue,
                "event_date": event.event_date,
                "when": format_when(event, display_tz),
                "gcal_url": google_calendar_url(event, display_tz),
                "url": event.url,
                "description": event.description,
                "score": rec.score,
                "relevance_notes": rec.relevance_notes,
                "feedback": fb.rating if fb else None,
            })
            if len(result) >= limit:
                break
        return result


async def _household_shared_events(
    limit: int = 20, display_tz: str = DEFAULT_TZ
) -> list[dict]:
    async with SessionFactory() as session:
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
        )
        now = datetime.now(UTC)
        result = []
        for event, rec in rows:
            if not is_upcoming(event, now):
                continue
            user_names = await session.execute(
                select(User.display_name)
                .join(Recommendation, Recommendation.user_id == User.id)
                .where(Recommendation.event_id == event.id, User.slug != HOUSEHOLD_SLUG)
                .distinct()
            )
            result.append({
                "id": event.id,
                "title": event.title,
                "venue": event.venue,
                "event_date": event.event_date,
                "when": format_when(event, display_tz),
                "gcal_url": google_calendar_url(event, display_tz),
                "url": event.url,
                "description": event.description,
                "score": rec.score,
                "who": [r[0] for r in user_names],
            })
            if len(result) >= limit:
                break
        return result


# --------------------------------------------------------------------------- #
# Personal user routes                                                         #
# --------------------------------------------------------------------------- #

@app.get("/u/{slug}/", response_class=HTMLResponse)
async def user_home(request: Request, slug: str):
    prefs, user = await _get_user_or_404(slug)
    display_tz = prefs.timezone or DEFAULT_TZ
    events: list[dict] = []
    if user:
        events = await _events_for_user(user.id, display_tz=display_tz)
    household = await _household_shared_events(limit=5, display_tz=display_tz)
    return templates.TemplateResponse(
        request,
        "user_home.html",
        {"prefs": prefs, "events": events, "household": household},
    )


@app.get("/u/{slug}/preferences", response_class=HTMLResponse)
async def user_prefs_page(request: Request, slug: str):
    prefs, _ = await _get_user_or_404(slug)
    return templates.TemplateResponse(
        request, "user_prefs.html", {"prefs": prefs}
    )


@app.post("/u/{slug}/preferences")
async def save_user_prefs(
    request: Request,
    slug: str,
    display_name: str = Form(""),
    email: str = Form(""),
    location: str = Form(""),
    timezone: str = Form("America/Los_Angeles"),
    interests: str = Form(""),
    blocklist: str = Form(""),
    frequency: str = Form("weekly"),
    day_of_week: str = Form("monday"),
    day_of_month: int = Form(1),
    hour: int = Form(8),
):
    all_prefs = load_all_prefs(settings.preferences_dir)
    if slug not in all_prefs:
        raise HTTPException(status_code=404)

    prefs = all_prefs[slug]
    prefs.display_name = display_name
    prefs.email = email
    prefs.location = location
    prefs.timezone = timezone
    prefs.interests = [i.strip() for i in interests.splitlines() if i.strip()]
    prefs.blocklist = [b.strip() for b in blocklist.splitlines() if b.strip()]
    prefs.schedule.frequency = frequency
    prefs.schedule.day_of_week = day_of_week
    prefs.schedule.day_of_month = day_of_month
    prefs.schedule.hour = hour

    save_prefs(prefs, settings.preferences_dir / f"{slug}.yaml")
    reload_scheduler(request.app.state.scheduler, SessionFactory, settings)
    return RedirectResponse(f"/u/{slug}/", status_code=303)


@app.post("/u/{slug}/feedback/{event_id}/{rating}")
async def submit_feedback(slug: str, event_id: int, rating: int):
    if rating not in (1, -1):
        raise HTTPException(status_code=400, detail="Rating must be 1 or -1")
    _, user = await _get_user_or_404(slug)
    if not user:
        raise HTTPException(status_code=404, detail="User not in DB yet — run a search first")

    async with SessionFactory() as session:
        async with session.begin():
            event = await session.get(Event, event_id)
            if not event:
                raise HTTPException(status_code=404, detail="Event not found")
            existing = await session.scalar(
                select(Feedback).where(
                    Feedback.event_id == event_id,
                    Feedback.user_id == user.id,
                )
            )
            if existing:
                existing.rating = rating
            else:
                session.add(Feedback(event_id=event_id, user_id=user.id, rating=rating))

    return RedirectResponse(f"/u/{slug}/", status_code=303)


@app.post("/u/{slug}/run")
async def trigger_user_run(slug: str, background_tasks: BackgroundTasks):
    all_prefs = load_all_prefs(settings.preferences_dir)
    if slug not in all_prefs:
        raise HTTPException(status_code=404)
    prefs = all_prefs[slug]
    background_tasks.add_task(
        run_for_user,
        prefs=prefs,
        session_factory=SessionFactory,
        settings=settings,
    )
    return RedirectResponse(f"/u/{slug}/", status_code=303)


@app.get("/u/{slug}/history", response_class=HTMLResponse)
async def user_history(request: Request, slug: str):
    prefs, user = await _get_user_or_404(slug)
    runs: list[Run] = []
    if user:
        async with SessionFactory() as session:
            result = await session.scalars(
                select(Run)
                .where(Run.user_id == user.id)
                .order_by(Run.started_at.desc())
                .limit(20)
            )
            runs = list(result)
    return templates.TemplateResponse(
        request, "user_history.html", {"prefs": prefs, "runs": runs}
    )


async def _user_events(user_id: int) -> list[Event]:
    """All events recommended to a user, excluding thumbs-downed ones."""
    async with SessionFactory() as session:
        disliked = (
            select(Feedback.event_id)
            .where(Feedback.user_id == user_id, Feedback.rating == -1)
            .scalar_subquery()
        )
        rows = await session.execute(
            select(Event)
            .join(Recommendation, Recommendation.event_id == Event.id)
            .where(Recommendation.user_id == user_id, Event.id.notin_(disliked))
            .distinct()
        )
        return list(rows.scalars())


async def _calendar_month(user_id: int, year: int, month: int, tz_name: str) -> dict:
    """Build a month grid of a user's recommended events (recurring expanded)."""
    tz = _safe_tz(tz_name)

    month_start = tz.localize(datetime(year, month, 1))
    last_day = _calendar.monthrange(year, month)[1]
    month_end = tz.localize(datetime(year, month, last_day, 23, 59, 59))

    # day-of-month -> list of event dicts
    by_day: dict[int, list[dict]] = {d: [] for d in range(1, last_day + 1)}
    for local_date, entry in occurrence_entries(
        await _user_events(user_id), month_start, month_end, tz_name
    ):
        by_day[local_date.day].append(entry)

    # Weeks as lists of dates (Sunday-first), spanning the month
    cal = _calendar.Calendar(firstweekday=6)
    weeks = cal.monthdatescalendar(year, month)

    return {
        "year": year,
        "month": month,
        "month_name": _calendar.month_name[month],
        "weeks": weeks,
        "by_day": by_day,
        "today": datetime.now(tz).date(),
    }


def _grouped_days(entries: list[tuple[date, dict]], today: date) -> list[dict]:
    """Group (date, entry) pairs into day sections for the agenda template."""
    groups: list[dict] = []
    for local_date, entry in entries:
        if not groups or groups[-1]["date"] != local_date:
            groups.append({
                "date": local_date,
                "label": local_date.strftime("%a, %b %-d"),
                "is_today": local_date == today,
                "events": [],
            })
        groups[-1]["events"].append(entry)
    return groups


def _half_open(
    entries: list[tuple[date, dict]], range_end: datetime
) -> list[tuple[date, dict]]:
    """Drop occurrences at exactly range_end — expansion is inclusive, but
    agenda windows are half-open so midnight events don't leak into the
    previous day's window."""
    return [pair for pair in entries if pair[1]["sort_key"] < range_end]


async def _user_agenda(user_id: int, start: date, days: int, tz_name: str) -> list[dict]:
    """Day-grouped agenda entries for a user over [start, start + days)."""
    tz = _safe_tz(tz_name)
    range_start = tz.localize(datetime(start.year, start.month, start.day))
    range_end = range_start + timedelta(days=days)
    entries = _half_open(
        occurrence_entries(
            await _user_events(user_id), range_start, range_end, tz_name
        ),
        range_end,
    )
    return _grouped_days(entries, datetime.now(tz).date())


async def _household_agenda(start: date, days: int, tz_name: str) -> list[dict]:
    """Day-grouped agenda for shared events plus household-run picks."""
    tz = _safe_tz(tz_name)
    range_start = tz.localize(datetime(start.year, start.month, start.day))
    range_end = range_start + timedelta(days=days)

    async with SessionFactory() as session:
        shared_ids = (
            select(Recommendation.event_id)
            .group_by(Recommendation.event_id)
            .having(func.count(Recommendation.user_id.distinct()) >= 2)
        ).scalar_subquery()

        household_user_id = await session.scalar(
            select(User.id).where(User.slug == HOUSEHOLD_SLUG)
        )
        conditions = [Event.id.in_(shared_ids)]
        if household_user_id is not None:
            conditions.append(
                Event.id.in_(
                    select(Recommendation.event_id)
                    .where(Recommendation.user_id == household_user_id)
                    .scalar_subquery()
                )
            )

        rows = await session.execute(select(Event).where(or_(*conditions)).distinct())
        events = list(rows.scalars())

        entries = _half_open(
            occurrence_entries(events, range_start, range_end, tz_name),
            range_end,
        )

        event_ids = {e.id for e in events}
        who_map: dict[int, list[str]] = {}
        if event_ids:
            who_rows = await session.execute(
                select(Recommendation.event_id, User.display_name)
                .join(User, User.id == Recommendation.user_id)
                .where(
                    Recommendation.event_id.in_(event_ids),
                    User.slug != HOUSEHOLD_SLUG,
                )
                .distinct()
            )
            for event_id, name in who_rows:
                who_map.setdefault(event_id, []).append(name)

    for _, entry in entries:
        entry["who"] = sorted(who_map.get(entry["id"], []))
    return _grouped_days(entries, datetime.now(tz).date())


def _parse_agenda_params(start: str | None, days: int | None, tz) -> tuple[date, int]:
    today = datetime.now(tz).date()
    try:
        start_date = date.fromisoformat(start) if start else today
    except (ValueError, TypeError):
        start_date = today
    return start_date, min(max(days or 14, 1), 60)


@app.get("/u/{slug}/calendar", response_class=HTMLResponse)
async def user_calendar(request: Request, slug: str, year: int | None = None, month: int | None = None):
    prefs, user = await _get_user_or_404(slug)
    tz_name = prefs.timezone or DEFAULT_TZ
    now_local = datetime.now(_safe_tz(tz_name))

    year = year or now_local.year
    month = month or now_local.month
    # Normalise out-of-range months
    if month < 1:
        month, year = 12, year - 1
    elif month > 12:
        month, year = 1, year + 1

    data = {"weeks": [], "by_day": {}, "year": year, "month": month,
            "month_name": _calendar.month_name[month], "today": now_local.date()}
    if user:
        data = await _calendar_month(user.id, year, month, tz_name)

    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month + 1 if month < 12 else 1
    next_year = year + 1 if month == 12 else year

    return templates.TemplateResponse(
        request,
        "calendar_month.html",
        {
            "prefs": prefs,
            "cal": data,
            "prev": {"year": prev_year, "month": prev_month},
            "next": {"year": next_year, "month": next_month},
        },
    )


@app.get("/u/{slug}/agenda", response_class=HTMLResponse)
async def user_agenda(
    request: Request,
    slug: str,
    start: str | None = None,
    days: int | None = None,
):
    prefs, user = await _get_user_or_404(slug)
    tz_name = prefs.timezone or DEFAULT_TZ
    tz = _safe_tz(tz_name)
    start_date, window = _parse_agenda_params(start, days, tz)

    groups = await _user_agenda(user.id, start_date, window, tz_name) if user else []

    return templates.TemplateResponse(
        request,
        "agenda.html",
        {
            "prefs": prefs,
            "is_household": False,
            "groups": groups,
            "window": window,
            "start": start_date,
            "prev": (start_date - timedelta(days=window)).isoformat(),
            "next": (start_date + timedelta(days=window)).isoformat(),
        },
    )


@app.get("/u/{slug}/calendar.ics")
async def user_ical(slug: str):
    ics = await generate_ical_for_user(SessionFactory, slug, only_recurring=None)
    return Response(content=ics, media_type="text/calendar")


@app.get("/u/{slug}/one-off.ics")
async def user_ical_one_off(slug: str):
    ics = await generate_ical_for_user(SessionFactory, slug, only_recurring=False)
    return Response(content=ics, media_type="text/calendar")


@app.get("/u/{slug}/recurring.ics")
async def user_ical_recurring(slug: str):
    ics = await generate_ical_for_user(SessionFactory, slug, only_recurring=True)
    return Response(content=ics, media_type="text/calendar")


# --------------------------------------------------------------------------- #
# Household routes                                                             #
# --------------------------------------------------------------------------- #

@app.get("/household/", response_class=HTMLResponse)
async def household_home(request: Request):
    all_prefs = load_all_prefs(settings.preferences_dir)
    household_prefs = all_prefs.get(HOUSEHOLD_SLUG)
    display_tz = (household_prefs.timezone if household_prefs else None) or DEFAULT_TZ
    events = await _household_shared_events(display_tz=display_tz)
    return templates.TemplateResponse(
        request,
        "household_home.html",
        {"prefs": household_prefs, "events": events},
    )


@app.get("/household/agenda", response_class=HTMLResponse)
async def household_agenda(
    request: Request,
    start: str | None = None,
    days: int | None = None,
):
    all_prefs = load_all_prefs(settings.preferences_dir)
    household_prefs = all_prefs.get(HOUSEHOLD_SLUG)
    tz_name = (household_prefs.timezone if household_prefs else None) or DEFAULT_TZ
    tz = _safe_tz(tz_name)
    start_date, window = _parse_agenda_params(start, days, tz)

    groups = await _household_agenda(start_date, window, tz_name)

    return templates.TemplateResponse(
        request,
        "agenda.html",
        {
            "prefs": household_prefs,
            "is_household": True,
            "groups": groups,
            "window": window,
            "start": start_date,
            "prev": (start_date - timedelta(days=window)).isoformat(),
            "next": (start_date + timedelta(days=window)).isoformat(),
        },
    )


@app.post("/household/run")
async def trigger_household_run(background_tasks: BackgroundTasks):
    all_prefs = load_all_prefs(settings.preferences_dir)
    household_prefs = all_prefs.get(HOUSEHOLD_SLUG)
    if not household_prefs:
        raise HTTPException(status_code=404, detail="No household.yaml found")
    background_tasks.add_task(
        run_for_user,
        prefs=household_prefs,
        session_factory=SessionFactory,
        settings=settings,
        all_prefs=all_prefs,
    )
    return RedirectResponse("/household/", status_code=303)


@app.post("/household/synthesize")
async def synthesize_household_prefs():
    all_prefs = load_all_prefs(settings.preferences_dir)
    user_prefs = [p for p in all_prefs.values() if not p.is_household]
    existing = all_prefs.get(HOUSEHOLD_SLUG)
    household = synthesize_household(user_prefs, existing=existing)
    save_prefs(household, settings.preferences_dir / "household.yaml")
    return RedirectResponse("/household/", status_code=303)


# --------------------------------------------------------------------------- #
# PWA manifest                                                                 #
# --------------------------------------------------------------------------- #

@app.get("/manifest.json")
async def manifest():
    from fastapi.responses import FileResponse
    return FileResponse(TEMPLATES_DIR / "manifest.json", media_type="application/manifest+json")


# --------------------------------------------------------------------------- #
# Root                                                                         #
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    all_prefs = load_all_prefs(settings.preferences_dir)
    users = [p for p in all_prefs.values() if not p.is_household]
    return templates.TemplateResponse(
        request, "index.html", {"users": users}
    )
