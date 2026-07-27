"""DB-integration tests for the Slack-specific query helper in queries.py."""
import pytest
from sqlmodel import Session, select

import queries
from app import app
from db import create_db_and_tables, engine, get_session
from entries import upsert_entries
from models import Entry


@pytest.fixture(scope="function")
def test_session():
    create_db_and_tables()
    with Session(engine) as session:
        yield session
        all_entries = session.exec(select(Entry)).all()
        for entry in all_entries:
            session.delete(entry)
        session.commit()


def test_get_last_week_entries_for_user_full_days(test_session):
    from schemas import EntryCreate

    # Last week: Mon Neal Street, Wed WFH. This week starts 2026-08-03 (Monday).
    last_week_start = "2026-07-27"
    upsert_entries(test_session, "Alice Johnson", [
        EntryCreate(date="2026-07-27", location="Neal Street"),
        EntryCreate(date="2026-07-29", location="WFH"),
    ])

    result = queries.get_last_week_entries_for_user(test_session, "alice johnson", "2026-08-03")

    assert result[0]["full"].location == "Neal Street"
    assert result[0]["morning"] is None
    assert result[2]["full"].location == "WFH"
    assert 1 not in result  # Tuesday had no entry


def test_build_prefill_includes_client_choice_for_client_office(test_session, monkeypatch):
    """Regression test: _build_prefill previously only returned "text" for a
    Client Office day, not "client_choice" -- since _build_day_blocks keys off
    client_choice (not text) to decide the dropdown's initial selection and
    whether to show the custom-name field, reopening /enter-week for a week with
    an existing Client Office entry showed the dropdown blank, silently losing
    the client name on save unless the user noticed and re-picked it."""
    import clients
    import slack_routes
    import slack_views
    from schemas import EntryCreate

    monkeypatch.setattr(clients, "get_clients", lambda: ["Sky", "FT"])

    upsert_entries(test_session, "Alice Johnson", [
        EntryCreate(date="2026-07-27", location="Client Office", client="Sky"),  # a listed client
        EntryCreate(date="2026-07-28", location="Client Office", client="Acme Ventures"),  # a custom one
    ])

    prefill = slack_routes._build_prefill(test_session, "alice johnson", "2026-07-27")

    assert prefill[0]["split"] is False
    assert prefill[0]["full"]["client_choice"] == "Sky"
    assert prefill[0]["full"]["text"] == "Sky"
    assert prefill[1]["full"]["client_choice"] == slack_views.CUSTOM_CLIENT_VALUE
    assert prefill[1]["full"]["text"] == "Acme Ventures"


def test_build_prefill_represents_a_split_day(test_session):
    """A split (morning/afternoon) day this week should pre-fill fully -- split
    checkbox on, both halves populated -- not be skipped."""
    import slack_routes
    from schemas import EntryCreate

    upsert_entries(test_session, "Bob Smith", [
        EntryCreate(date="2026-07-27", time_period="Morning", location="Neal Street"),
        EntryCreate(date="2026-07-27", time_period="Afternoon", location="WFH"),
    ])

    prefill = slack_routes._build_prefill(test_session, "bob smith", "2026-07-27")

    assert prefill[0]["split"] is True
    assert prefill[0]["morning"]["location"] == "Neal Street"
    assert prefill[0]["afternoon"]["location"] == "WFH"


def test_get_last_week_entries_for_user_separates_split_days(test_session):
    """A split (morning/afternoon) day should show up under morning/afternoon,
    not full -- the Slack modal's prefill logic (slack_routes.py) uses this
    distinction to fully represent a split day, checkbox pre-checked."""
    from schemas import EntryCreate

    upsert_entries(test_session, "Bob Smith", [
        EntryCreate(date="2026-07-27", time_period="Morning", location="Neal Street"),
        EntryCreate(date="2026-07-27", time_period="Afternoon", location="WFH"),
    ])

    result = queries.get_last_week_entries_for_user(test_session, "bob smith", "2026-08-03")

    assert result[0]["full"] is None
    assert result[0]["morning"].location == "Neal Street"
    assert result[0]["afternoon"].location == "WFH"
