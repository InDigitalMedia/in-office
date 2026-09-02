"""Read-side query helpers shared by the HTTP routes and the Slack daily job.

Extracted from app.py so the daily Slack job can call these in-process instead of
making a self-HTTP round trip to its own API.
"""
import logging
from datetime import datetime, timedelta

from sqlmodel import Session
from sqlalchemy import text

from db_utils import check_time_period_column_exists, create_entry_from_row, latest_user_names, normalize_time_period
from entries import NEAL_STREET_BIKE_CAP
from schemas import SummaryRow

logger = logging.getLogger(__name__)


def _week_end(week_start: str) -> str:
    start_date = datetime.strptime(week_start, "%Y-%m-%d").date()
    return (start_date + timedelta(days=4)).strftime("%Y-%m-%d")


def get_week_entries(session: Session, week_start: str) -> list[SummaryRow]:
    """All entries for the Mon-Fri week starting on week_start."""
    end_date = _week_end(week_start)
    time_period_exists = check_time_period_column_exists()

    if time_period_exists:
        result = session.execute(text("""
            SELECT id, user_key, user_name, date, location,
                   NULLIF(time_period, '') as time_period,
                   client, notes, extra, extra_note, created_at, updated_at
            FROM entry
            WHERE date >= :start_date AND date <= :end_date
            ORDER BY date, user_name, time_period
        """), {"start_date": week_start, "end_date": end_date})
        entries = [create_entry_from_row(row, include_time_period=True) for row in result.fetchall()]
    else:
        result = session.execute(text("""
            SELECT id, user_key, user_name, date, location, client, notes, created_at, updated_at
            FROM entry
            WHERE date >= :start_date AND date <= :end_date
            ORDER BY date, user_name
        """), {"start_date": week_start, "end_date": end_date})
        entries = [create_entry_from_row(row, include_time_period=False) for row in result.fetchall()]

    return [
        SummaryRow(
            user_name=entry.user_name,
            date=entry.date,
            location=entry.location,
            time_period=normalize_time_period(getattr(entry, 'time_period', None)),
            client=entry.client,
            notes=entry.notes,
            extra=getattr(entry, 'extra', None),
            extra_note=getattr(entry, 'extra_note', None),
        )
        for entry in entries
    ]


def get_submitted_users(session: Session, week_start: str) -> list[str]:
    """Display names of everyone who has at least one entry for this Mon-Fri week."""
    end_date = _week_end(week_start)
    time_period_exists = check_time_period_column_exists()

    if time_period_exists:
        from sqlmodel import select
        from models import Entry
        stmt = select(Entry).where(Entry.date >= week_start, Entry.date <= end_date)
        entries = session.exec(stmt).all()
    else:
        result = session.execute(text("""
            SELECT id, user_key, user_name, date, location, client, notes, created_at, updated_at
            FROM entry
            WHERE date >= :start_date AND date <= :end_date
        """), {"start_date": week_start, "end_date": end_date})
        entries = [create_entry_from_row(row, include_time_period=False) for row in result.fetchall()]

    return latest_user_names(entries)


def get_full_bike_dates(session: Session, week_start: str, exclude_user_key: str) -> set:
    """Dates within the Mon-Fri week starting at week_start where Neal Street's
    bike cap is already reached by people other than exclude_user_key -- used to
    hide/grey out the "Bike" option for those dates in both the web app and the
    Slack modal, so the cap is visible before someone tries to save rather than
    only as a rejection."""
    end_date = _week_end(week_start)
    result = session.execute(text("""
        SELECT date FROM entry
        WHERE date >= :start_date AND date <= :end_date
          AND location = 'Neal Street' AND extra = 'Bike' AND user_key != :user_key
        GROUP BY date
        HAVING COUNT(DISTINCT user_key) >= :cap
    """), {"start_date": week_start, "end_date": end_date, "user_key": exclude_user_key, "cap": NEAL_STREET_BIKE_CAP})
    return {row[0] for row in result.fetchall()}


def get_last_week_entries_for_user(session: Session, user_key: str, week_start: str) -> dict:
    """This user's entries for the week immediately before week_start, grouped by
    weekday offset (0=Mon..4=Fri). Mirrors the day-offset mapping the web app does
    client-side in fillFromLastWeek (frontend/src/App.tsx:790-864).

    Returns {offset: {"full": SummaryRow | None, "morning": SummaryRow | None, "afternoon": SummaryRow | None}}
    """
    start_date = datetime.strptime(week_start, "%Y-%m-%d").date()
    prev_week_start = (start_date - timedelta(days=7)).strftime("%Y-%m-%d")

    week_rows = get_week_entries(session, prev_week_start)
    prev_start_date = datetime.strptime(prev_week_start, "%Y-%m-%d").date()

    by_offset: dict[int, dict] = {}
    for row in week_rows:
        if row.user_name.strip().lower() != user_key:
            continue
        row_date = datetime.strptime(row.date, "%Y-%m-%d").date()
        offset = (row_date - prev_start_date).days
        if offset < 0 or offset > 4:
            continue
        slot = by_offset.setdefault(offset, {"full": None, "morning": None, "afternoon": None})
        if row.time_period == "Morning":
            slot["morning"] = row
        elif row.time_period == "Afternoon":
            slot["afternoon"] = row
        else:
            slot["full"] = row

    return by_offset
