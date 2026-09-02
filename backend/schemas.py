from datetime import datetime

from pydantic import BaseModel, field_validator, model_validator
from sqlmodel import SQLModel


VALID_EXTRAS = {"Bike", "Pet", "Other"}


class EntryCreate(BaseModel):
    date: str  # YYYY-MM-DD format
    location: str
    time_period: str | None = None  # 'Morning', 'Afternoon', or None for full day
    client: str | None = None
    notes: str | None = None
    extra: str | None = None  # 'Bike', 'Pet', 'Other', or None for "nothing extra"
    extra_note: str | None = None  # free-text description, required when extra == 'Other'

    @field_validator("location")
    @classmethod
    def validate_location(cls, v):
        # Accept both new and legacy names and normalize to new names
        legacy_map = {
            "Office": "Neal Street",
            "Client": "Client Office",
            "Off": "Holiday",
            "PTO": "Holiday",
        }
        normalized = legacy_map.get(v, v)
        valid_locations = {"Neal Street", "WFH", "Client Office", "Holiday", "Working From Abroad", "Other"}
        if normalized not in valid_locations:
            raise ValueError(f"Location must be one of: {valid_locations}")
        return normalized

    @field_validator("extra")
    @classmethod
    def validate_extra(cls, v):
        if v is not None and v not in VALID_EXTRAS:
            raise ValueError(f"extra must be one of: {VALID_EXTRAS}")
        return v

    @model_validator(mode="after")
    def validate_client(self):
        if self.location in {"Client Office", "Client"} and not self.client:
            raise ValueError("Client name is required when location is 'Client Office'")
        if self.location == "Other" and not self.client:
            raise ValueError("Location description is required when location is 'Other'")
        if self.extra and self.location != "Neal Street":
            raise ValueError("extra info can only be set when location is 'Neal Street'")
        if self.extra == "Other" and not self.extra_note:
            raise ValueError("extra_note is required when extra is 'Other'")
        return self


class BulkUpsertRequest(BaseModel):
    user_name: str
    entries: list[EntryCreate]


class BulkUpsertResponse(BaseModel):
    ok: bool
    count: int


class SummaryRow(BaseModel):
    user_name: str
    date: str
    location: str
    time_period: str | None = None  # 'Morning', 'Afternoon', or None for full day
    client: str | None = None
    notes: str | None = None
    extra: str | None = None
    extra_note: str | None = None


class WeekSummaryResponse(BaseModel):
    entries: list[SummaryRow]


class AdminTabPasswordRequest(BaseModel):
    password: str


class EntryResponse(SQLModel):
    id: int
    user_name: str
    date: str
    location: str
    time_period: str | None = None
    client: str | None = None
    notes: str | None = None
    extra: str | None = None
    extra_note: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
