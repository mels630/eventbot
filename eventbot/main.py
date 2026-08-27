from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
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


async def _events_for_user(
    user_id: int,
    is_household: bool = False,
    limit: int = 20,
) -> list[dict]:
    async with SessionFactory() as session:
        rows = await session.execute(
            select(Event, Recommendation)
            .join(Recommendation, Recommendation.event_id == Event.id)
            .where(
                Recommendation.user_id == user_id,
                Recommendation.is_household == is_household,
            )
            .order_by(Recommendation.score.desc())
            .limit(limit)
        )
        result = []
        for event, rec in rows:
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
                "url": event.url,
                "description": event.description,
                "score": rec.score,
                "relevance_notes": rec.relevance_notes,
                "feedback": fb.rating if fb else None,
            })
        return result


async def _household_shared_events(limit: int = 20) -> list[dict]:
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
            .limit(limit)
        )
        result = []
        for event, rec in rows:
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
                "url": event.url,
                "description": event.description,
                "score": rec.score,
                "who": [r[0] for r in user_names],
            })
        return result


# --------------------------------------------------------------------------- #
# Personal user routes                                                         #
# --------------------------------------------------------------------------- #

@app.get("/u/{slug}/", response_class=HTMLResponse)
async def user_home(request: Request, slug: str):
    prefs, user = await _get_user_or_404(slug)
    events: list[dict] = []
    if user:
        events = await _events_for_user(user.id)
    household = await _household_shared_events(limit=5)
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
    display_name: str = Form(...),
    email: str = Form(...),
    location: str = Form(...),
    timezone: str = Form(...),
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
    events = await _household_shared_events()
    return templates.TemplateResponse(
        request,
        "household_home.html",
        {"prefs": household_prefs, "events": events},
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
