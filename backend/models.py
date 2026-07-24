from datetime import UTC, datetime

from sqlmodel import Field, SQLModel, UniqueConstraint


class Entry(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("user_key", "date", "time_period", name="uniq_entries_userkey_date_timeperiod"),)

    id: int | None = Field(default=None, primary_key=True)
    user_key: str = Field(index=True)  # Normalized: lower(trim(user_name))
    user_name: str = Field(index=True)  # Display name (preserves casing)
    date: str = Field(index=True)  # YYYY-MM-DD format
    location: str = Field(index=True)
    time_period: str = Field(default="", index=True)  # 'Morning', 'Afternoon', or '' for full day
    client: str | None = Field(default=None)
    notes: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = Field(default=None)


class ScheduledRunLog(SQLModel, table=True):
    """Tracks the last London-local date each scheduled Slack job actually
    sent. GitHub Actions' cron scheduler can fire hours late (or not at all,
    or -- via the redundant BST/GMT-safe double firing -- twice in one day if
    the gate window is wide enough to catch both), so daily_notifications.py's
    gate checks this before sending and records it after, to guarantee at most
    one real send per job per day regardless of how many times or how late the
    trigger actually arrives."""
    job_name: str = Field(primary_key=True)
    last_run_date: str  # YYYY-MM-DD, London-local
