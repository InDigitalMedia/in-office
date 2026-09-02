"""Row-parsing / user-identity helpers shared by app.py and queries.py.

Extracted from app.py (rather than importing from it) so queries.py can use these
without a circular import once app.py imports queries.py.
"""
import logging

from sqlmodel import Session
from sqlalchemy import text

from db import engine

logger = logging.getLogger(__name__)

# Cache for time_period column check
_time_period_exists = None


def create_entry_from_row(row, include_time_period: bool = False) -> object:
    """Create an Entry-like object from a database row.

    Args:
        row: Database row tuple
        include_time_period: If True, expects time_period as row[5], shifts other fields
    """
    entry = type('Entry', (), {})()
    entry.id = row[0]
    entry.user_key = row[1]
    entry.user_name = row[2]
    entry.date = row[3]
    entry.location = row[4]
    if include_time_period and len(row) > 8:
        # Row includes time_period + extras: id, user_key, user_name, date, location, time_period, client, notes, extra, extra_note, created_at, updated_at
        # Normalize empty string to None for consistency
        entry.time_period = None if (not row[5] or row[5] == '') else row[5]
        entry.client = row[6]
        entry.notes = row[7]
        entry.extra = row[8]
        entry.extra_note = row[9]
        entry.created_at = row[10]
        entry.updated_at = row[11]
    else:
        # Row doesn't include time_period: id, user_key, user_name, date, location, client, notes, created_at, updated_at
        entry.time_period = None
        entry.client = row[5]
        entry.notes = row[6]
        entry.extra = None
        entry.extra_note = None
        entry.created_at = row[7]
        entry.updated_at = row[8]
    return entry


def check_time_period_column_exists(session: Session = None) -> bool:
    """Check if time_period column exists in entry table."""
    global _time_period_exists
    if _time_period_exists is not None:
        return _time_period_exists

    try:
        is_postgres = "postgresql" in str(engine.url).lower()
        with engine.connect() as conn:
            if is_postgres:
                result = conn.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'entry' AND column_name = 'time_period'
                """))
                _time_period_exists = result.fetchone() is not None
            else:
                result = conn.execute(text("PRAGMA table_info(entry)"))
                columns = [row[1] for row in result.fetchall()]
                _time_period_exists = 'time_period' in columns
    except Exception as e:
        logger.warning(f"Could not check time_period column: {e}")
        _time_period_exists = False

    return _time_period_exists


def normalize_time_period(value: str | None) -> str | None:
    """Empty string (how the DB stores 'full day') normalizes to None for the API contract."""
    return value or None


def latest_user_names(entries) -> list[str]:
    """Given a list of entries, return sorted unique display names, preferring the
    most-recently-updated casing per user_key."""
    latest_name: dict[str, str] = {}
    latest_ts: dict = {}
    for entry in entries:
        key = entry.user_key
        updated_at = getattr(entry, "updated_at", None)
        if key not in latest_name:
            latest_name[key] = entry.user_name
            if updated_at:
                latest_ts[key] = updated_at
        elif updated_at and (key not in latest_ts or updated_at > latest_ts[key]):
            latest_name[key] = entry.user_name
            latest_ts[key] = updated_at
    return sorted(set(latest_name.values()))
