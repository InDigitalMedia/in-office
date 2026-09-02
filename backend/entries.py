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

NEAL_STREET_BIKE_CAP = 2


def _check_bike_capacity(session: Session, user_key: str, entries: list[EntryCreate], is_postgres: bool) -> None:
    """Raises ValueError if this batch would put a 3rd distinct person's bike at
    Neal Street on any single date. The cap is per whole day (not per morning/
    afternoon split), and doesn't count this same user against themselves --
    resubmitting/editing your own already-booked bike day must never self-block.
    """
    bike_dates = {e.date for e in entries if e.location == "Neal Street" and e.extra == "Bike"}
    if not bike_dates:
        return

    if is_postgres:
        # Serializes the check-then-write below against other users' concurrent
        # submissions for the same date -- without this, two different people's
        # requests could both pass the count check before either commits,
        # letting a 3rd (or 4th) bike through. Keyed per-date, separate from the
        # per-user lock above, since this race is specifically cross-user.
        # Sorted so two overlapping multi-date submissions (e.g. one for Mon+Wed,
        # another for Wed+Mon) always acquire their locks in the same relative
        # order -- iterating a plain set here would order by hash, which varies
        # with the set's contents and can have two submissions each hold one
        # date's lock while waiting on the other's, deadlocking both.
        for date in sorted(bike_dates):
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"bike:{date}"})

    for date in sorted(bike_dates):
        count = session.execute(
            text("""
                SELECT COUNT(DISTINCT user_key) FROM entry
                WHERE date = :date AND location = 'Neal Street' AND extra = 'Bike' AND user_key != :user_key
            """),
            {"date": date, "user_key": user_key},
        ).scalar() or 0
        if count >= NEAL_STREET_BIKE_CAP:
            raise ValueError(
                f"Bike capacity full at Neal Street on {date} -- {NEAL_STREET_BIKE_CAP} bikes already booked. "
                f"No days were saved -- please remove that day's bike and resubmit."
            )


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
        except Exception as e:
            # Falling back to the SQLite branch here also silently drops the
            # per-user advisory lock below and the per-date bike lock in
            # _check_bike_capacity -- on an actual Postgres deployment that
            # reopens the exact interleaving race the per-user lock exists to
            # prevent, so this needs to be visible rather than a bare `pass`.
            logger.warning(f"Could not determine DB dialect, defaulting to SQLite pattern: {e}")

        if is_postgres:
            # Serialize concurrent upserts for the same user so one request's delete-then-
            # insert (below) can't run interleaved with another's -- without this, two
            # near-simultaneous submissions for the same user+date (e.g. a full-day one and
            # a split one) can each fail to see the other's uncommitted row under READ
            # COMMITTED, leaving both a full-day and a split row behind. Held for the
            # transaction's duration and auto-released on commit/rollback.
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:user_key))"), {"user_key": user_key})

        _check_bike_capacity(session, user_key, entries, is_postgres)

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
                        INSERT INTO entry (user_key, user_name, date, location, time_period, client, notes, extra, extra_note, created_at, updated_at)
                        VALUES (:user_key, :user_name, :date, :location, :time_period, :client, :notes, :extra, :extra_note, :created_at, :updated_at)
                        ON CONFLICT (user_key, date, time_period) DO UPDATE
                        SET user_name = EXCLUDED.user_name,
                            location = EXCLUDED.location,
                            client = EXCLUDED.client,
                            notes = EXCLUDED.notes,
                            extra = EXCLUDED.extra,
                            extra_note = EXCLUDED.extra_note,
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
                        "extra": entry_data.extra,
                        "extra_note": entry_data.extra_note,
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
                    existing.extra = entry_data.extra
                    existing.extra_note = entry_data.extra_note
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
                        extra=entry_data.extra,
                        extra_note=entry_data.extra_note,
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

    except ValueError:
        # A genuine validation failure (empty entries, bike capacity) -- re-raise
        # as-is so callers keep mapping ValueError to a 400, distinct from an
        # infrastructure failure below.
        session.rollback()
        raise
    except Exception as e:
        # Anything else (a dropped connection, a syntax error) is not a user
        # mistake -- previously this was flattened into the same ValueError as
        # above, which made a bike-cap rejection and a broken DB connection
        # indistinguishable to callers (both became a 400). Re-raising the
        # original exception type lets it surface as a 500 instead.
        session.rollback()
        logger.error(f"Error in bulk upsert: {str(e)}")
        raise
