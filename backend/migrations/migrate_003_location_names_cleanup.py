"""
Migration: Rename old location values to their current names, and drop PTO entries
(the PTO location option was removed from the app).

Previously exposed as a live POST /admin/migrate-locations endpoint -- moved here
because a standing, repeatable HTTP endpoint that permanently deletes rows is a
data-loss risk (re-invoking it later would silently drop any newly-created PTO
rows). This is a one-shot script instead: run it manually once, like migrate_001/002.

Idempotent: once the old names are renamed, the UPDATEs match zero rows on a
re-run; once PTO rows are deleted, the DELETE matches zero rows too.
"""
import logging
from sqlalchemy import text

logger = logging.getLogger(__name__)

MIGRATION_MAP = {
    "Office": "Neal Street",
    "Client": "Client Office",
    "Off": "Holiday",
}


def migrate(engine):
    """Run migration."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            updated_count = 0
            for old_name, new_name in MIGRATION_MAP.items():
                result = conn.execute(
                    text("UPDATE entry SET location = :new_name WHERE location = :old_name"),
                    {"new_name": new_name, "old_name": old_name},
                )
                updated_count += result.rowcount or 0

            result = conn.execute(text("DELETE FROM entry WHERE location = :old_name"), {"old_name": "PTO"})
            deleted_count = result.rowcount or 0

            trans.commit()
            logger.info(
                f"✅ Migration 003 completed: {updated_count} renamed, {deleted_count} PTO entries deleted"
            )
        except Exception as e:
            trans.rollback()
            logger.error(f"❌ Migration 003 failed: {str(e)}")
            raise


if __name__ == "__main__":
    from db import engine
    migrate(engine)
