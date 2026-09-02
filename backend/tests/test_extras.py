"""Tests for the "extra info" (Bike/Pet/Other) feature and the Neal Street bike cap."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import app
from db import create_db_and_tables, engine, get_session
from models import Entry
from schemas import EntryCreate


@pytest.fixture(scope="function")
def test_session():
    """Create a test database session."""
    create_db_and_tables()
    with Session(engine) as session:
        yield session
        # Clean up all test data after test
        all_entries = session.exec(select(Entry)).all()
        for entry in all_entries:
            session.delete(entry)
        session.commit()


@pytest.fixture(scope="function")
def client(test_session):
    """Create a test client with dependency override."""
    def get_test_session():
        yield test_session

    app.dependency_overrides[get_session] = get_test_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --- EntryCreate validation ---------------------------------------------------

def test_entry_create_rejects_invalid_extra_value():
    with pytest.raises(Exception):
        EntryCreate(date="2024-02-01", location="Neal Street", extra="Scooter")


def test_entry_create_requires_extra_note_when_other():
    with pytest.raises(Exception):
        EntryCreate(date="2024-02-01", location="Neal Street", extra="Other")

    # Providing the note makes it valid
    entry = EntryCreate(date="2024-02-01", location="Neal Street", extra="Other", extra_note="Bringing a package")
    assert entry.extra == "Other"
    assert entry.extra_note == "Bringing a package"


def test_entry_create_accepts_bike_and_pet_without_note():
    bike = EntryCreate(date="2024-02-01", location="Neal Street", extra="Bike")
    assert bike.extra == "Bike"
    pet = EntryCreate(date="2024-02-01", location="Neal Street", extra="Pet")
    assert pet.extra == "Pet"


def test_entry_create_rejects_extra_outside_neal_street():
    """Extra info (bike/pet/etc.) only makes sense at Neal Street -- any other
    location must reject it outright, rather than silently accepting and
    storing something that can never be entered through either UI."""
    for location in ("WFH", "Client Office", "Holiday", "Working From Abroad", "Other"):
        with pytest.raises(Exception):
            EntryCreate(date="2024-02-01", location=location, client="Acme", extra="Bike")


# --- Persistence ---------------------------------------------------------------

def test_extra_and_extra_note_persist_through_bulk_upsert(client):
    request_data = {
        "user_name": "Extra Test User",
        "entries": [
            {"date": "2024-02-05", "location": "Neal Street", "extra": "Bike"},
            {"date": "2024-02-06", "location": "Neal Street", "extra": "Other", "extra_note": "Vet appointment"},
        ],
    }
    response = client.post("/entries/bulk_upsert", json=request_data)
    assert response.status_code == 200

    entries_response = client.get("/entries?date_from=2024-02-05&date_to=2024-02-06")
    entries = {e["date"]: e for e in entries_response.json()}
    assert entries["2024-02-05"]["extra"] == "Bike"
    assert entries["2024-02-05"]["extra_note"] is None
    assert entries["2024-02-06"]["extra"] == "Other"
    assert entries["2024-02-06"]["extra_note"] == "Vet appointment"


# --- Neal Street bike cap --------------------------------------------------------

def _book_bike(client, user_name, date="2024-03-04"):
    return client.post("/entries/bulk_upsert", json={
        "user_name": user_name,
        "entries": [{"date": date, "location": "Neal Street", "extra": "Bike"}],
    })


def test_third_bike_at_neal_street_is_blocked(client):
    assert _book_bike(client, "Bike Rider One").status_code == 200
    assert _book_bike(client, "Bike Rider Two").status_code == 200

    response = _book_bike(client, "Bike Rider Three")
    assert response.status_code == 400
    assert "capacity" in response.json()["detail"].lower()

    # Only the first two bikes actually got saved
    entries_response = client.get("/entries?date_from=2024-03-04&date_to=2024-03-04")
    entries = entries_response.json()
    assert len(entries) == 2


def test_resubmitting_own_bike_day_is_not_self_blocked(client):
    assert _book_bike(client, "Bike Rider One").status_code == 200
    assert _book_bike(client, "Bike Rider Two").status_code == 200

    # Bike Rider One edits/resaves their own day (e.g. adds a note) -- must not
    # be blocked by their own existing booking.
    response = client.post("/entries/bulk_upsert", json={
        "user_name": "Bike Rider One",
        "entries": [{"date": "2024-03-04", "location": "Neal Street", "extra": "Bike", "notes": "still biking"}],
    })
    assert response.status_code == 200


def test_pet_and_other_are_not_capped_at_neal_street(client):
    assert _book_bike(client, "Bike Rider One").status_code == 200
    assert _book_bike(client, "Bike Rider Two").status_code == 200

    # A 3rd, 4th, 5th person bringing a pet or something else isn't blocked --
    # the cap only applies to Bike.
    response = client.post("/entries/bulk_upsert", json={
        "user_name": "Pet Owner",
        "entries": [{"date": "2024-03-04", "location": "Neal Street", "extra": "Pet"}],
    })
    assert response.status_code == 200


def test_bike_cap_is_per_date_not_global(client):
    assert _book_bike(client, "Bike Rider One", date="2024-03-04").status_code == 200
    assert _book_bike(client, "Bike Rider Two", date="2024-03-04").status_code == 200

    # A different date starts a fresh count.
    response = _book_bike(client, "Bike Rider Three", date="2024-03-05")
    assert response.status_code == 200


def test_extra_is_rejected_via_api_outside_neal_street(client):
    """Extra info can only be entered for Neal Street -- the API must reject
    it for any other location. EntryCreate is validated as part of request
    parsing (it's a nested field of BulkUpsertRequest), so this surfaces as
    FastAPI's own 422, not the app's 400 (used for logic-level rejections like
    the bike cap, which only run once the request body has already parsed)."""
    response = client.post("/entries/bulk_upsert", json={
        "user_name": "Home Biker",
        "entries": [{"date": "2024-03-04", "location": "WFH", "extra": "Bike"}],
    })
    assert response.status_code == 422
