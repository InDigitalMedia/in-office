"""
Migration: Add extra/extra_note columns for per-day "extra info" (Bike/Pet/Other).

Adds two nullable TEXT columns to entry -- no constraint or data changes, so
this is safe to run repeatedly and never touches existing rows.

Unlike migrate_002_add_time_period, there is no runtime fallback for these
columns being absent -- every read (queries.py) and write (entries.py) path
unconditionally references extra/extra_note. So this deliberately does NOT
follow migrate_002's catch-and-log-and-continue pattern: a failure here must
crash app startup (see app.py's lifespan calling this outside the broader
migrations try/except) rather than let the app come up "healthy" and then
500 on every save and every week-summary read.
"""
import logging
from sqlalchemy import text

logger = logging.getLogger(__name__)


def is_postgres(engine):
    return "postgresql" in str(engine.url).lower()


def migrate(engine):
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            if is_postgres(engine):
                migrate_postgres(conn)
            else:
                migrate_sqlite(conn)
            trans.commit()
            logger.info("✅ Migration 004 completed successfully")
        except Exception as e:
            trans.rollback()
            logger.error(f"❌ Migration 004 failed: {str(e)}")
            raise


def migrate_postgres(conn):
    logger.info("Running PostgreSQL migration for extra/extra_note...")
    result = conn.execute(text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'entry' AND column_name IN ('extra', 'extra_note')
        AND table_schema = current_schema()
    """))
    existing_columns = {row[0] for row in result.fetchall()}
    if {'extra', 'extra_note'} <= existing_columns:
        logger.info("extra/extra_note columns already exist, skipping migration")
        return

    if 'extra' not in existing_columns:
        conn.execute(text("ALTER TABLE entry ADD COLUMN extra TEXT"))
    if 'extra_note' not in existing_columns:
        conn.execute(text("ALTER TABLE entry ADD COLUMN extra_note TEXT"))
    logger.info("✅ extra/extra_note columns added")


def migrate_sqlite(conn):
    logger.info("Running SQLite migration for extra/extra_note...")
    result = conn.execute(text("""
        SELECT name FROM sqlite_master WHERE type='table' AND name='entry'
    """))
    if not result.fetchone():
        logger.info("Entry table does not exist, skipping migration")
        return

    result = conn.execute(text("PRAGMA table_info(entry)"))
    columns = {row[1] for row in result.fetchall()}
    if {'extra', 'extra_note'} <= columns:
        logger.info("extra/extra_note columns already exist, skipping migration")
        return

    if 'extra' not in columns:
        conn.execute(text("ALTER TABLE entry ADD COLUMN extra TEXT"))
    if 'extra_note' not in columns:
        conn.execute(text("ALTER TABLE entry ADD COLUMN extra_note TEXT"))
    logger.info("✅ extra/extra_note columns added")


if __name__ == "__main__":
    from db import engine
    migrate(engine)
