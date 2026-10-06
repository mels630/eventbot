"""Tests for the idempotent column migration in main._apply_column_migrations."""
import pytest
from sqlalchemy import create_engine, inspect, text

from eventbot.main import _apply_column_migrations


def _columns(conn, table):
    return {c["name"] for c in inspect(conn).get_columns(table)}


def test_migration_adds_missing_column(tmp_path):
    db = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db}")
    # Simulate an old schema: events table WITHOUT end_date.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE events ("
            " id INTEGER PRIMARY KEY,"
            " title VARCHAR, title_slug VARCHAR, venue VARCHAR,"
            " event_date VARCHAR, url VARCHAR)"
        ))

    with engine.begin() as conn:
        assert "end_date" not in _columns(conn, "events")
        _apply_column_migrations(conn)
        cols = _columns(conn, "events")
        assert "end_date" in cols
        assert "end_at" in cols

    # Idempotent: running again is a no-op and does not raise.
    with engine.begin() as conn:
        _apply_column_migrations(conn)
        assert "end_date" in _columns(conn, "events")
    engine.dispose()


def test_migration_skips_absent_table(tmp_path):
    db = tmp_path / "empty.db"
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        # No 'events' table at all -> should not raise.
        _apply_column_migrations(conn)
    engine.dispose()
