import logging
import os
from sqlmodel import Session, SQLModel, create_engine

logger = logging.getLogger(__name__)

# Get database URL from environment, default to SQLite for local dev
# Use persistent storage path if running in container with volume mount
db_path = os.getenv("DATABASE_PATH", "./worktracker.db")
env = os.getenv("ENV", os.getenv("RENDER", "").lower() or "dev")

if os.getenv("DATABASE_URL"):
    DATABASE_URL = os.getenv("DATABASE_URL")
else:
    # Guard against SQLite fallback in production
    if env in ("prod", "production") or os.getenv("RENDER"):
        raise RuntimeError(
            "DATABASE_URL missing in production; refusing to start with SQLite. "
            "Please configure DATABASE_URL environment variable."
        )
    DATABASE_URL = f"sqlite:///{db_path}"

# Render provides postgres://, which SQLAlchemy doesn't accept. Name the driver
# explicitly too: SQLAlchemy 2.1+ maps a bare postgresql:// to psycopg (v3),
# but requirements.txt installs psycopg2, so a bare URL crashes on startup.
for _prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL and DATABASE_URL.startswith(_prefix):
        DATABASE_URL = DATABASE_URL.replace(_prefix, "postgresql+psycopg2://", 1)
        break

# Log database driver for observability
db_driver = DATABASE_URL.split(":", 1)[0] if ":" in DATABASE_URL else "unknown"
logger.info(f"DB_URL_DRIVER={db_driver}")

# Create engine
engine = create_engine(DATABASE_URL, echo=False)


def create_db_and_tables():
    """Create database and tables if they don't exist.
    This is safe to call multiple times - it won't wipe existing data.
    """
    SQLModel.metadata.create_all(engine)


def get_session():
    """Get database session."""
    with Session(engine) as session:
        yield session
