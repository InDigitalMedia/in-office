"""Core entry-write logic, shared by the HTTP bulk_upsert route and the Slack integration.

Extracted from app.py's bulk_upsert_entries so both callers share the exact same
transactional behavior -- this logic was previously fixed for a delete/insert
atomicity bug, and a second, drifted reimplementation would risk reintroducing it.
"""
import logging
from datetime import UTC, datetime

from sqlmodel import Session, select
from sqlalchemy import text

from db import engine
from models import Entry
from schemas import EntryCreate

logger = logging.getLogger(__name__)


def upsert_entries(session: Session, user_name: str, entries: list[EntryCreate]) -> int:
    """Upsert a batch of entries for a user, atomically, in a single transaction.

    Raises ValueError if entries is empty or on any underlying error (caller decides
    how to surface it -- HTTPException for the web route, Slack error response for
    the modal path).
    """
    if not entries:
        raise ValueError("No entries provided")

    user_key = user_name.strip().lower()
    logger.info(f"Bulk upsert request for user_key: {user_key} (display: {user_name})")

    try:
        count = 0

        # time_period always exists by the time requests are served (migration 002
        # runs on every app startup, before lifespan yields) -- the legacy
        # no-time_period code paths this once branched on have been removed.
        is_postgres = False
        try:
            if hasattr(session.bind, 'url'):
                is_postgres = "postgresql" in str(session.bind.url).lower()
            else:
                # Fallback: check engine URL
                is_postgres = "postgresql" in str(engine.url).lower()
        except Exception:
            pass  # Default to SQLite pattern

        if is_postgres:
            # Serialize concurrent upserts for the same user so one request's delete-then-
            # insert (below) can't run interleaved with another's -- without this, two
            # near-simultaneous submissions for the same user+date (e.g. a full-day one and
            # a split one) can each fail to see the other's uncommitted row under READ
            # COMMITTED, leaving both a full-day and a split row behind. Held for the
            # transaction's duration and auto-released on commit/rollback.
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:user_key))"), {"user_key": user_key})

        # Handle overwriting between split and full-day entries
        # Collect dates that have split entries (time_period is not None/empty)
        split_dates = set()
        # Collect dates that have full-day entries (time_period is None/empty)
        full_day_dates = set()

        for entry_data in entries:
            if entry_data.time_period and entry_data.time_period.strip():
                split_dates.add(entry_data.date)
            else:
                # time_period is None or empty string - this is a full-day entry
                full_day_dates.add(entry_data.date)

        # Delete old full-day entries for dates that now have split entries
        if split_dates:
            logger.info(f"Deleting old full-day entries for split dates: {split_dates}")
            placeholders = ','.join([':date' + str(i) for i in range(len(split_dates))])
            params = {"user_key": user_key}
            for i, date in enumerate(split_dates):
                params[f"date{i}"] = date
            session.execute(
                text(f"""
                    DELETE FROM entry
                    WHERE user_key = :user_key
                    AND date IN ({placeholders})
                    AND (time_period = '' OR time_period IS NULL)
                """),
                params
            )

        # Delete old split entries (Morning/Afternoon) for dates that now have full-day entries
        if full_day_dates:
            logger.info(f"Deleting old split entries for full-day dates: {full_day_dates}")
            placeholders = ','.join([':date' + str(i) for i in range(len(full_day_dates))])
            params = {"user_key": user_key}
            for i, date in enumerate(full_day_dates):
                params[f"date{i}"] = date
            session.execute(
                text(f"""
                    DELETE FROM entry
                    WHERE user_key = :user_key
                    AND date IN ({placeholders})
                    AND (time_period != '' AND time_period IS NOT NULL)
                """),
                params
            )

        for entry_data in entries:
            # Validate entry
            if not entry_data.date:
                continue

            # Use current timestamp for created_at/updated_at
            now = datetime.now(UTC)
            # Normalize None to empty string for consistency with migration
            time_period_value = entry_data.time_period if entry_data.time_period is not None else ''

            if is_postgres:
                logger.info(f"Saving entry: date={entry_data.date}, location={entry_data.location}, time_period={time_period_value}")
                result = session.execute(
                    text("""
                        INSERT INTO entry (user_key, user_name, date, location, time_period, client, notes, created_at, updated_at)
                        VALUES (:user_key, :user_name, :date, :location, :time_period, :client, :notes, :created_at, :updated_at)
                        ON CONFLICT (user_key, date, time_period) DO UPDATE
                        SET user_name = EXCLUDED.user_name,
                            location = EXCLUDED.location,
                            client = EXCLUDED.client,
                            notes = EXCLUDED.notes,
                            updated_at = EXCLUDED.updated_at
                    """),
                    {
                        "user_key": user_key,
                        "user_name": user_name.strip(),
                        "date": entry_data.date,
                        "location": entry_data.location,
                        "time_period": time_period_value,
                        "client": entry_data.client,
                        "notes": entry_data.notes,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                count += result.rowcount if result.rowcount else 1
            else:
                # SQLite: Use ORM merge pattern (select, update or insert)
                existing = session.exec(
                    select(Entry)
                    .where(Entry.user_key == user_key)
                    .where(Entry.date == entry_data.date)
                    .where(Entry.time_period == time_period_value)
                ).first()

                if existing:
                    existing.user_name = user_name.strip()
                    existing.location = entry_data.location
                    existing.time_period = time_period_value
                    existing.client = entry_data.client
                    existing.notes = entry_data.notes
                    existing.updated_at = now
                else:
                    new_entry = Entry(
                        user_key=user_key,
                        user_name=user_name.strip(),
                        date=entry_data.date,
                        location=entry_data.location,
                        time_period=time_period_value,
                        client=entry_data.client,
                        notes=entry_data.notes,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(new_entry)
                count += 1

        # Single commit for all operations (atomic)
        session.commit()

        logger.info(
            f"Successfully upserted {count} entries for user_key: {user_key} "
            f"(display: {user_name})"
        )
        return count

    except Exception as e:
        session.rollback()
        logger.error(f"Error in bulk upsert: {str(e)}")
        raise ValueError(str(e)) from e
