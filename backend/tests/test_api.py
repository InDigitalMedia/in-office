import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete

from app import app
from db import create_db_and_tables, engine, get_session
from models import Entry


@pytest.fixture(scope="function")
def test_session():
    """Create a test database session."""
    create_db_and_tables()
    with Session(engine) as session:
        yield session
        # Clean up all test data after test
        from sqlmodel import select
        all_entries = session.exec(select(Entry)).all()
        for entry in all_entries:
            session.delete(entry)
        session.commit()


@pytest.fixture(scope="function")
def client(test_session, monkeypatch):
    """Create a test client with dependency override."""
    monkeypatch.setattr("app.ADMIN_SECRET", "test-secret")
    monkeypatch.setattr("app.ADMIN_TAB_PASSWORD", "test-tab-password")

    def get_test_session():
        yield test_session

    app.dependency_overrides[get_session] = get_test_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_bulk_upsert_success(client):
    """Test successful bulk upsert."""
    request_data = {
        "user_name": "test_user",
        "entries": [
            {"date": "2024-01-15", "location": "Neal Street", "notes": "Test day"},
            {"date": "2024-01-16", "location": "WFH", "notes": "Work from home"},
            {
                "date": "2024-01-17",
                "location": "Client Office",
                "client": "Test Client",
                "notes": "Client meeting",
            },
        ],
    }

    response = client.post("/entries/bulk_upsert", json=request_data)
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["count"] == 3


def test_bulk_upsert_client_validation(client):
    """Test validation when client location is missing client name."""
    request_data = {
        "user_name": "test_user",
        "entries": [
            {"date": "2024-01-15", "location": "Client Office", "notes": "Missing client name"}
        ],
    }

    response = client.post("/entries/bulk_upsert", json=request_data)
    assert response.status_code == 422  # Validation error


def test_bulk_upsert_invalid_location(client):
    """Test validation with invalid location."""
    request_data = {
        "user_name": "test_user",
        "entries": [
            {
                "date": "2024-01-15",
                "location": "InvalidLocation",
                "notes": "Invalid location",
            }
        ],
    }

    response = client.post("/entries/bulk_upsert", json=request_data)
    assert response.status_code == 422  # Validation error


def test_week_summary_success(client):
    """Test successful week summary retrieval."""
    # First, add some test data
    request_data = {
        "user_name": "test_user",
        "entries": [
            {"date": "2024-01-15", "location": "Neal Street", "notes": "Monday"},
            {"date": "2024-01-16", "location": "WFH", "notes": "Tuesday"},
        ],
    }
    client.post("/entries/bulk_upsert", json=request_data)

    # Get week summary
    response = client.get("/summary/week?week_start=2024-01-15")
    assert response.status_code == 200
    data = response.json()
    assert len(data["entries"]) == 2
    assert data["entries"][0]["user_name"] == "test_user"
    assert data["entries"][0]["date"] == "2024-01-15"


def test_week_summary_invalid_date(client):
    """Test week summary with invalid date format."""
    response = client.get("/summary/week?week_start=invalid-date")
    assert response.status_code == 400


def test_get_entries_with_filters(client):
    """Test getting entries with date filters."""
    # Add test data
    request_data = {
        "user_name": "test_user",
        "entries": [
            {"date": "2024-01-15", "location": "Neal Street", "notes": "Monday"},
            {"date": "2024-01-20", "location": "WFH", "notes": "Saturday"},
        ],
    }
    client.post("/entries/bulk_upsert", json=request_data)

    # Test with date filter
    response = client.get("/entries?date_from=2024-01-15&date_to=2024-01-16")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["date"] == "2024-01-15"


def test_delete_entry_success(client):
    """Test successful entry deletion."""
    # First, add test data
    request_data = {
        "user_name": "test_user",
        "entries": [{"date": "2024-01-15", "location": "Neal Street", "notes": "Monday"}],
    }
    response = client.post("/entries/bulk_upsert", json=request_data)
    assert response.status_code == 200

    # Get the entry ID
    entries_response = client.get("/entries")
    assert entries_response.status_code == 200
    entries = entries_response.json()
    assert len(entries) == 1
    entry_id = entries[0]["id"]

    # Delete the entry (admin-gated -- see require_admin in app.py)
    response = client.delete(f"/entries/{entry_id}", headers={"X-Admin-Secret": "test-secret"})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True


def test_delete_entry_not_found(client):
    """Test deleting non-existent entry."""
    response = client.delete("/entries/99999", headers={"X-Admin-Secret": "test-secret"})
    assert response.status_code == 404


def test_root_endpoint(client):
    """Test root endpoint."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert "docs" in data


def test_admin_tab_password_correct_password_succeeds(client):
    response = client.post("/admin/verify-tab-password", json={"password": "test-tab-password"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_admin_tab_password_wrong_password_rejected(client):
    response = client.post("/admin/verify-tab-password", json={"password": "wrong"})
    assert response.status_code == 401


def test_admin_tab_password_not_configured(client, monkeypatch):
    monkeypatch.setattr("app.ADMIN_TAB_PASSWORD", None)
    response = client.post("/admin/verify-tab-password", json={"password": "anything"})
    assert response.status_code == 503


def test_vercel_preview_cors_regex_matches_current_project_name():
    """Regression test: this regex hardcodes the Vercel PROJECT name
    (vercel.com/in-digital/in-office), which is independent of the GitHub
    repo's name -- if either gets renamed again without updating this, every
    future PR preview silently loses the ability to reach the API (this
    already happened once with the old "work-location-tracker" project name)."""
    import re

    from app import _vercel_preview_origin_regex

    assert re.match(_vercel_preview_origin_regex, "https://in-office-git-some-branch-in-digital.vercel.app")
    assert re.match(_vercel_preview_origin_regex, "https://in-office-git-fix-thing-a1b2c3-in-digital.vercel.app")
    assert not re.match(_vercel_preview_origin_regex, "https://in-office.vercel.app")  # production -- exact-match list, not this regex
    assert not re.match(_vercel_preview_origin_regex, "https://evil.vercel.app")
