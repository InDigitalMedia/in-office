import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select
from sqlalchemy import text

import entries as entries_module
from db import create_db_and_tables, get_session, engine
from models import Entry
from schemas import (
    AdminTabPasswordRequest,
    BulkUpsertRequest,
    BulkUpsertResponse,
    EntryResponse,
    SummaryRow,
    WeekSummaryResponse,
)
from slack_routes import slack_router

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import queries
from db_utils import check_time_period_column_exists, create_entry_from_row, latest_user_names, normalize_time_period

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup."""
    create_db_and_tables()
    
    # Run migrations if needed
    try:
        migrations_path = os.path.join(os.path.dirname(__file__), 'migrations')
        if os.path.exists(migrations_path):
            # Run migration 001: Add user_key constraint
            try:
                from migrations.migrate_001_add_user_key_constraint import migrate as migrate_001
                migrate_001(engine)
            except ImportError as e:
                logger.debug(f"Migration 001 module not found: {e}")
            except Exception as e:
                logger.warning(f"Migration 001 check failed (may already be applied): {str(e)}")
            
            # Run migration 002: Add time_period
            try:
                logger.info("Attempting to run migration 002 (time_period)...")
                from migrations.migrate_002_add_time_period import migrate as migrate_002
                migrate_002(engine)
                logger.info("Migration 002 completed (check logs above for details)")
            except ImportError as e:
                logger.debug(f"Migration 002 module not found: {e}")
            except Exception as e:
                logger.error(f"Migration 002 failed: {str(e)}")
                # Don't raise - allow app to start, but log the error clearly
                import traceback
                logger.error(f"Migration 002 traceback: {traceback.format_exc()}")
    except Exception as e:
        logger.warning(f"Migration check failed: {str(e)}")
    
    logger.info("Database initialized")
    yield


# Create FastAPI app
app = FastAPI(title="Work Location Tracker API", version="1.0.0", lifespan=lifespan)

# Add CORS middleware — restricted to known frontend origins (override/extend via
# the comma-separated CORS_ORIGINS env var for other deploys) plus this Vercel
# project's preview deployments, which get a unique per-branch/per-commit hostname
# that can't be listed individually (e.g. in-office-git-<branch>-in-digital.vercel.app).
# The prefix here is the Vercel PROJECT name (vercel.com/in-digital/in-office),
# which is independent of the GitHub repo's name -- if either is renamed again,
# this needs updating to match, or every future PR preview loses API access.
_default_cors_origins = "https://in-office.vercel.app,http://localhost:5173,http://localhost:4173"
allowed_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", _default_cors_origins).split(",")
    if origin.strip()
]
_vercel_preview_origin_regex = os.getenv(
    "CORS_ORIGIN_REGEX", r"^https://in-office-[a-z0-9-]+-in-digital\.vercel\.app$"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=_vercel_preview_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(slack_router)

ADMIN_SECRET = os.getenv("ADMIN_SECRET")


def require_admin(x_admin_secret: str | None = Header(default=None)):
    """Gate admin/utility routes behind a shared secret set via the ADMIN_SECRET env var."""
    if not ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Admin endpoints are not configured")
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret")


# Gates the frontend's Admin tab -- a password typed into the UI, not sent as a
# header automatically, so it's a separate env var/endpoint from ADMIN_SECRET
# above. Set via the ADMIN_TAB_PASSWORD env var (temporary password, expected
# to be rotated/removed later); no code-committed default, since this repo is
# public.
ADMIN_TAB_PASSWORD = os.getenv("ADMIN_TAB_PASSWORD")


@app.post("/admin/verify-tab-password")
def verify_admin_tab_password(request: AdminTabPasswordRequest):
    if not ADMIN_TAB_PASSWORD:
        raise HTTPException(status_code=503, detail="Admin tab password is not configured")
    if not hmac.compare_digest(request.password, ADMIN_TAB_PASSWORD):
        raise HTTPException(status_code=401, detail="Incorrect password")
    return {"ok": True}


@app.post("/entries/bulk_upsert", response_model=BulkUpsertResponse)
def bulk_upsert_entries(
    request: BulkUpsertRequest, session: Session = Depends(get_session)
):
    """Bulk upsert entries for a user using atomic per-day upserts (no destructive deletes)."""
    if not request.entries:
        raise HTTPException(status_code=400, detail="No entries provided")

    try:
        count = entries_module.upsert_entries(session, request.user_name, request.entries)
        return BulkUpsertResponse(ok=True, count=count)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/summary/week", response_model=WeekSummaryResponse)
def get_week_summary(
    week_start: str = Query(..., description="Week start date in YYYY-MM-DD format"),
    session: Session = Depends(get_session),
):
    """Get all entries for a week starting from the given date."""
    logger.info(f"Week summary request for week starting: {week_start}")

    try:
        summary_rows = queries.get_week_entries(session, week_start)
        logger.info(f"Found {len(summary_rows)} entries for week {week_start}")
        return WeekSummaryResponse(entries=summary_rows)
    except ValueError as e:
        logger.error(f"Invalid date format: {str(e)}")
        raise HTTPException(
            status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
        ) from e
    except Exception as e:
        logger.error(f"Error getting week summary: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/entries", response_model=list[EntryResponse])
def get_entries(
    date_from: str = Query(None, description="Start date filter (YYYY-MM-DD)"),
    date_to: str = Query(None, description="End date filter (YYYY-MM-DD)"),
    session: Session = Depends(get_session),
):
    """Get entries with optional date filtering."""
    logger.info(f"Entries request - from: {date_from}, to: {date_to}")

    try:
        time_period_exists = check_time_period_column_exists()
        
        if time_period_exists:
            # Use model query if column exists
            stmt = select(Entry)

            if date_from:
                stmt = stmt.where(Entry.date >= date_from)
            if date_to:
                stmt = stmt.where(Entry.date <= date_to)

            stmt = stmt.order_by(Entry.date, Entry.user_name)
            entries = session.exec(stmt).all()
        else:
            # Use raw SQL if column doesn't exist
            sql = "SELECT id, user_key, user_name, date, location, client, notes, created_at, updated_at FROM entry WHERE 1=1"
            params = {}
            if date_from:
                sql += " AND date >= :date_from"
                params["date_from"] = date_from
            if date_to:
                sql += " AND date <= :date_to"
                params["date_to"] = date_to
            sql += " ORDER BY date, user_name"
            
            result = session.execute(text(sql), params)
            rows = result.fetchall()
            # Convert to Entry-like objects
            entries = [create_entry_from_row(row) for row in rows]

        return [
            EntryResponse(
                id=entry.id,
                user_name=entry.user_name,
                date=entry.date,
                location=entry.location,
                time_period=normalize_time_period(getattr(entry, 'time_period', None)),
                client=entry.client,
                notes=entry.notes,
                created_at=entry.created_at,
                updated_at=entry.updated_at,
            )
            for entry in entries
        ]

    except Exception as e:
        logger.error(f"Error getting entries: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/entries/user-week")
def delete_user_week(
    user_name: str = Query(..., description="User name whose entries should be deleted"),
    week_start: str = Query(..., description="Week start date in YYYY-MM-DD format"),
    session: Session = Depends(get_session),
):
    """Delete all of a user's entries for a given week (Monday-Friday)."""
    logger.info(f"Delete week request for user: {user_name}, week starting: {week_start}")

    try:
        start_date = datetime.strptime(week_start, "%Y-%m-%d").date()
        end_date = start_date + timedelta(days=4)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD") from e

    if start_date.weekday() != 0:
        raise HTTPException(status_code=400, detail="week_start must be a Monday")

    user_key = user_name.strip().lower()

    try:
        stmt = select(Entry).where(
            Entry.user_key == user_key,
            Entry.date >= week_start,
            Entry.date <= end_date.strftime("%Y-%m-%d"),
        )
        entries = session.exec(stmt).all()

        if not entries:
            raise HTTPException(status_code=404, detail="No entries found for this user and week")

        for entry in entries:
            session.delete(entry)
        session.commit()

        logger.info(f"Deleted {len(entries)} entries for user_key {user_key}, week {week_start}")
        return {"ok": True, "message": "Entries deleted successfully", "count": len(entries)}

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.error(f"Error deleting user week entries: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/entries/{entry_id}", dependencies=[Depends(require_admin)])
def delete_entry(entry_id: int, session: Session = Depends(get_session)):
    """Delete a specific entry by ID. Admin/utility only — not called by the frontend."""
    logger.info(f"Delete entry request for ID: {entry_id}")

    try:
        stmt = select(Entry).where(Entry.id == entry_id)
        entry = session.exec(stmt).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Entry not found")

        session.delete(entry)
        session.commit()

        logger.info(f"Successfully deleted entry {entry_id}")
        return {"ok": True, "message": "Entry deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.error(f"Error deleting entry: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/summary/all-users")
def get_all_users(
    session: Session = Depends(get_session),
):
    """Get list of all unique users who have ever submitted entries."""
    logger.info("All users request")

    try:
        # Query all entries to get unique user names
        time_period_exists = check_time_period_column_exists()
        
        if time_period_exists:
            # Use model query if column exists
            entries = session.exec(select(Entry)).all()
        else:
            # Use raw SQL if column doesn't exist
            result = session.execute(text("""
                SELECT id, user_key, user_name, date, location, client, notes, created_at, updated_at
                FROM entry
            """))
            rows = result.fetchall()
            entries = [create_entry_from_row(row) for row in rows]

        users = latest_user_names(entries)

        logger.info(f"Found {len(users)} total users")
        return {"users": users}

    except Exception as e:
        logger.error(f"Error getting all users: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/summary/users")
def get_users_for_week(
    week_start: str = Query(..., description="Week start date in YYYY-MM-DD format"),
    session: Session = Depends(get_session),
):
    """Get list of unique users who have entries for a given week."""
    logger.info(f"Users request for week starting: {week_start}")

    try:
        users = queries.get_submitted_users(session, week_start)
        logger.info(f"Found {len(users)} users for week {week_start}")
        return {"users": users}
    except ValueError as e:
        logger.error(f"Invalid date format: {str(e)}")
        raise HTTPException(
            status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
        )
    except Exception as e:
        logger.error(f"Error getting users: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/entries/check")
def check_existing_entries(
    user_name: str = Query(..., description="User name to check"),
    week_start: str = Query(..., description="Week start date in YYYY-MM-DD format"),
    session: Session = Depends(get_session),
):
    """Check if a user already has entries for a given week (uses normalized user_key)."""
    logger.info(f"Check entries request for user: {user_name}, week: {week_start}")
    
    try:
        # Calculate week end date (Friday)
        start_date = datetime.strptime(week_start, "%Y-%m-%d").date()
        end_date = start_date + timedelta(days=4)
        
        # Normalize user name to user_key
        user_key = user_name.strip().lower()
        
        # Check if time_period column exists
        time_period_exists = check_time_period_column_exists()
        
        if time_period_exists:
            # Use model query if column exists
            user_entries = session.exec(
                select(Entry)
                .where(Entry.user_key == user_key)
                .where(Entry.date >= week_start)
                .where(Entry.date <= end_date.strftime("%Y-%m-%d"))
            ).all()
            
            return {
                "exists": len(user_entries) > 0,
                "count": len(user_entries),
                "entries": [
                    {
                        "date": e.date,
                        "location": e.location,
                        "time_period": normalize_time_period(getattr(e, 'time_period', None)),
                        "client": e.client,
                        "notes": e.notes,
                    }
                    for e in user_entries
                ]
            }
        else:
            # Use raw SQL if column doesn't exist yet
            result = session.execute(text("""
                SELECT date, location, client, notes
                FROM entry
                WHERE user_key = :user_key
                AND date >= :start_date
                AND date <= :end_date
            """), {
                "user_key": user_key,
                "start_date": week_start,
                "end_date": end_date.strftime("%Y-%m-%d")
            })
            rows = result.fetchall()
            
            return {
                "exists": len(rows) > 0,
                "count": len(rows),
                "entries": [
                    {
                        "date": row[0],
                        "location": row[1],
                        "time_period": None,
                        "client": row[2],
                        "notes": row[3],
                    }
                    for row in rows
                ]
            }
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid date format")
    except Exception as e:
        logger.error(f"Error checking entries: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/debug", dependencies=[Depends(require_admin)])
def debug_database(session: Session = Depends(get_session)):
    """Debug endpoint to check database contents and connection."""
    try:
        from db import DATABASE_URL

        db_type = "PostgreSQL" if "postgresql://" in DATABASE_URL or "postgres://" in DATABASE_URL else "SQLite"

        # Try to get database name/info (sanitized for security)
        db_info = "unknown"
        if "@" in DATABASE_URL:
            # Show only the host part, not credentials
            db_info = DATABASE_URL.split("@")[-1].split("?")[0]
        elif "sqlite" in DATABASE_URL:
            db_info = DATABASE_URL.split("/")[-1]

        time_period_exists = check_time_period_column_exists()

        # Full column list (name + type) — Postgres only, best-effort for debugging
        try:
            with engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_name = 'entry'
                    ORDER BY ordinal_position
                """))
                table_columns = [{"name": row[0], "type": row[1]} for row in result.fetchall()]
        except Exception:
            table_columns = []

        # Aggregate stats computed in SQL instead of loading the whole table into Python
        total_count = session.execute(text("SELECT COUNT(*) FROM entry")).scalar() or 0

        min_date, max_date = session.execute(text("SELECT MIN(date), MAX(date) FROM entry")).one()

        users = [
            row[0]
            for row in session.execute(
                text("SELECT DISTINCT user_name FROM entry ORDER BY user_name")
            ).fetchall()
        ]

        time_period_select = "NULLIF(time_period, '') as time_period" if time_period_exists else "NULL as time_period"
        recent_rows = session.execute(text(f"""
            SELECT id, user_name, date, location, {time_period_select}, client
            FROM entry
            ORDER BY id DESC
            LIMIT 10
        """)).fetchall()
        recent_entries = [
            {
                "id": row[0],
                "user_name": row[1],
                "date": row[2],
                "location": row[3],
                "time_period": row[4],
                "client": row[5],
            }
            for row in recent_rows
        ]

        # Test if we can write (just verify connection works)
        connection_ok = True
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as conn_e:
            connection_ok = False
            logger.error(f"Connection test failed: {str(conn_e)}")

        # Check if we can query time_period (will fail if column doesn't exist)
        time_period_query_works = False
        if time_period_exists:
            try:
                sample_with_time_period = session.exec(
                    select(Entry).limit(1)
                ).first()
                if sample_with_time_period:
                    _ = sample_with_time_period.time_period  # Try to access it
                    time_period_query_works = True
            except Exception:
                time_period_query_works = False

        return {
            "database_type": db_type,
            "database_info": db_info,
            "connection_ok": connection_ok,
            "time_period_column_exists": time_period_exists,
            "time_period_query_works": time_period_query_works,
            "table_columns": table_columns,
            "total_entries": total_count,
            "unique_users": users,
            "date_range": {
                "earliest": min_date,
                "latest": max_date
            },
            "sample_entries": recent_entries
        }
    except Exception as e:
        logger.error(f"Debug error: {str(e)}")
        import traceback
        return {
            "error": str(e), 
            "traceback": traceback.format_exc(),
            "error_type": str(e.__class__.__name__)
        }


@app.get("/")
def root():
    """Root endpoint."""
    return {"message": "Work Location Tracker API", "docs": "/docs"}
