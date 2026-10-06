# CLAUDE.md — eventbot

A self-hosted event recommendation service. An AI agent (Claude Sonnet + Tavily web search) wakes on a schedule per user, discovers upcoming events matching their preferences, emails a digest, and learns from thumbs-up/down feedback. Supports 2–6 household members with independent preferences and a shared household mode.

## Stack

| Concern | Choice |
|---|---|
| Language | Python 3.11+ |
| Web framework | FastAPI + Jinja2 templates |
| Scheduler | APScheduler 3.x (in-process, async) |
| LLM | Claude Sonnet (`claude-sonnet-4-6`) via Anthropic SDK |
| Web search | Tavily API |
| DB | SQLite + SQLAlchemy async (`aiosqlite`) |
| User config | YAML files (one per user) |
| Email | aiosmtplib + SMTP relay |
| Deployment | Docker Compose on TrueNAS SCALE |

## Project layout

```
eventbot/
├── eventbot/
│   ├── main.py       # FastAPI app, lifespan (DB init + scheduler start), all routes
│   ├── agent.py      # Claude agent loop, Tavily tool use, deduplication, DB persistence
│   ├── models.py     # SQLAlchemy ORM models
│   ├── prefs.py      # YAML preference loading/saving, household synthesis
│   ├── scheduler.py  # APScheduler job builder, per-user cron triggers
│   ├── email.py      # SMTP digest sender, loads events from DB
│   ├── settings.py   # Pydantic settings (env vars → typed config)
│   └── templates/    # Jinja2: base.html, user_home/prefs/history, household_home,
│                     #         email_personal/household, index, manifest.json
├── tests/
│   ├── test_prefs.py   # YAML I/O, synthesize_household
│   ├── test_agent.py   # EventCandidate, _title_slug, _query_hash
│   └── test_models.py  # SQLAlchemy model creation + constraint checks
├── data/               # Runtime data (gitignored except examples)
│   └── preferences/    # User YAML files; filename stem = user slug
├── pyproject.toml
├── Dockerfile
└── docker-compose.yml
```

## Key data model

**Events are globally deduplicated** — they belong to no single user.

```
users           ← slug (from YAML filename), display_name
events          ← globally unique on (venue, event_date, title_slug)
recommendations ← join: (event_id, user_id, run_id, score, is_household)
feedback        ← (event_id, user_id, rating)  rating: 1=up, -1=down
runs            ← per-user job history (user_id=NULL for household)
search_cache    ← (query_hash, results_json, cached_at)  TTL=6h
```

"Shared" events are those with 2+ distinct `user_id` rows in `recommendations`. The `/household/` page queries for these — no special flag needed. The `is_household` bool on `Recommendation` is also set by the explicit household agent run and by `promote_shared_events()`.

## User preferences (YAML)

```
data/preferences/{slug}.yaml   →  UserPrefs  (loaded by prefs.py)
data/preferences/household.yaml  →  is_household=True  (special slug)
```

Fields: `display_name`, `email`, `location`, `timezone`, `interests[]`, `blocklist[]`, `schedule{frequency, day_of_week, day_of_month, hour}`.

`slug` is not stored in the file — it is derived from the filename stem at load time. `is_household` is set programmatically, not stored.

`synthesize_household()` builds a household profile as the union of all users' interests and blocklists.

## Agent flow (`agent.py`)

1. Build system prompt: user prefs + feedback history summary + date window
2. Enter tool-use loop with two tools: `web_search` and `finish_with_events`
3. `web_search` → `_cached_search()` → Tavily (or SQLite cache hit)
4. Agent generates 6–10 targeted queries, collects candidates
5. Agent calls `finish_with_events` with ranked list → loop exits
6. `persist_recommendations()` deduplicates events on `(venue, date, title_slug)`, upserts `Event` rows, inserts `Recommendation` rows
7. After individual runs: `promote_shared_events()` sets `is_household=True` on any event recommended to 2+ users

For household runs, `all_user_prefs` is passed in; the system prompt instructs the agent to score against each member's preferences.

## Scheduler (`scheduler.py`)

- `build_scheduler()` reads all YAML files, creates one APScheduler `CronTrigger` job per user (including household)
- `_cron_trigger()` maps `schedule.frequency` → APScheduler cron args
- `reload_scheduler()` removes all `run_*` jobs and rebuilds — called after preferences are saved via the UI
- Each job calls `run_for_user()`, which opens a DB session, runs the agent, persists results, then calls `send_digest()`

## Routes (`main.py`)

```
GET  /                              → index (user picker)
GET  /u/{slug}/                     → user home (events + household section)
GET  /u/{slug}/preferences          → preferences form
POST /u/{slug}/preferences          → save prefs, reload scheduler
POST /u/{slug}/feedback/{id}/{±1}   → submit rating
POST /u/{slug}/run                  → trigger background run
GET  /u/{slug}/history              → run history
GET  /household/                    → shared event feed
POST /household/run                 → trigger household background run
POST /household/synthesize          → regenerate household.yaml from user files
GET  /manifest.json                 → PWA manifest
```

All routes use `SessionFactory` (module-level `async_sessionmaker`) and `settings` (module-level `Settings` instance).

## Settings (`settings.py`)

Loaded from environment variables (or `data/.env` via docker-compose `env_file`):

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API |
| `TAVILY_API_KEY` | Web search |
| `SMTP_HOST/PORT/USERNAME/PASSWORD/FROM` | Email sending |
| `DATA_DIR` | Root of persistent data (default `/data`) |

`settings.db_path` → `{DATA_DIR}/eventbot.db`
`settings.preferences_dir` → `{DATA_DIR}/preferences/`

## Running tests

```bash
pip install -e ".[dev]"
pytest
```

Tests use in-memory SQLite and do not require API keys. 13 tests cover: prefs load/save/synthesize, EventCandidate parsing, slug/hash helpers, model creation and unique constraints.

## Common tasks

**Add a user:** create `data/preferences/{slug}.yaml`, restart container (or call `reload_scheduler` in tests).

**Change a user's schedule:** edit their YAML or use `/u/{slug}/preferences` in the UI — the form POSTs back and calls `reload_scheduler()` automatically.

**Inspect the DB:**
```bash
sqlite3 data/eventbot.db ".tables"
sqlite3 data/eventbot.db "SELECT u.slug, count(r.id) FROM recommendations r JOIN users u ON r.user_id=u.id GROUP BY u.slug;"
```

**Force a run without waiting for the schedule:**
```bash
curl -X POST http://localhost:8080/u/{slug}/run
```

## Design decisions worth preserving

- **No ML for the feedback loop.** Past ratings are summarised as plain text and injected into the agent's system prompt. Simple and effective.
- **Events are global, not per-user.** The `recommendations` join table is the per-user association. This makes "shared event" queries trivial and avoids duplicate event rows.
- **Search cache prevents redundant Tavily calls** when users share interests and runs happen close together. TTL is 6h — short enough that a manual re-run later in the day still gets fresh results.
- **YAML for config, SQLite for data.** Preferences are user-facing config that should be hand-editable and version-controllable. Event history and feedback are operational data that belong in a DB.
- **Household synthesis is a union, not an intersection.** The goal is to surface things any member might enjoy, not only universal overlaps.
