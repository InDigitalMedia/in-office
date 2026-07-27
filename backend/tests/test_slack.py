"""Unit tests for the Slack integration's non-DB-dependent logic:
signature verification, directory matching, modal submission parsing, and the
DST-safe scheduling gate. Deliberately doesn't hit the real Slack API or a
running Slack workspace -- those paths are covered by manual verification
(see the plan's Verification section).
"""
import hashlib
import hmac
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import clients
import daily_notifications
import slack_client
import slack_directory
import slack_routes
import slack_views


@pytest.fixture(autouse=True)
def _stub_clients_list(monkeypatch):
    """Every test in this module should be network-independent -- clients.py
    otherwise makes a real HTTP call the first time it's used."""
    monkeypatch.setattr(clients, "get_clients", lambda: ["Sky", "FT", "Mail"])


# --- slack_client.verify_signature -----------------------------------------
# verify_signature implements Slack's documented v0:{timestamp}:{body} HMAC-SHA256
# scheme (https://api.slack.com/authentication/verifying-requests-from-slack).
# These tests compute the expected signature independently (same documented
# formula, separate code path from the implementation under test) rather than
# relying on a hand-transcribed "official" example that can't be verified offline.

def _sign(secret: str, timestamp: str, body: bytes) -> str:
    base_string = f"v0:{timestamp}:{body.decode('utf-8')}"
    return "v0=" + hmac.new(secret.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha256).hexdigest()


def test_verify_signature_valid(monkeypatch):
    signing_secret = "8f742231b10e8888abcd99yyyzzz85a5"
    timestamp = "1531420618"
    body = b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&command=%2Fwebhook-collect"
    signature = _sign(signing_secret, timestamp, body)

    # Freeze "now" right next to the timestamp so the replay-protection age
    # check doesn't reject an otherwise-valid signature.
    monkeypatch.setattr(slack_client.time, "time", lambda: 1531420618 + 1)

    assert slack_client.verify_signature(timestamp, body, signature, signing_secret) is True


def test_verify_signature_tampered_body(monkeypatch):
    signing_secret = "8f742231b10e8888abcd99yyyzzz85a5"
    timestamp = "1531420618"
    body = b"token=xyzz0WbapA4vBCDEFasx0q6G"
    signature = _sign(signing_secret, timestamp, body)

    monkeypatch.setattr(slack_client.time, "time", lambda: 1531420618 + 1)

    tampered_body = b"token=something-else-entirely"
    assert slack_client.verify_signature(timestamp, tampered_body, signature, signing_secret) is False


def test_verify_signature_expired_timestamp():
    """A signature computed correctly for a very old timestamp should still be
    rejected -- the freshness check runs regardless of HMAC correctness."""
    signing_secret = "8f742231b10e8888abcd99yyyzzz85a5"
    timestamp = "1531420618"  # real "now" is years past this -- no monkeypatch here
    body = b"token=xyzz0WbapA4vBCDEFasx0q6G"
    signature = _sign(signing_secret, timestamp, body)

    assert slack_client.verify_signature(timestamp, body, signature, signing_secret) is False


# --- slack_directory.match_roster --------------------------------------------

def test_match_roster_exact_and_variance_and_unmatched():
    directory = {
        "alice johnson": {"id": "U001", "real_name": "Alice Johnson"},
        "bob smith": {"id": "U002", "real_name": "Bob Smith"},
    }
    roster_names = ["Alice Johnson", "  Bob Smith  ", "Ghost Person"]

    matched, unmatched = slack_directory.match_roster(roster_names, directory)

    assert matched == {"Alice Johnson": "U001", "  Bob Smith  ": "U002"}
    assert unmatched == ["Ghost Person"]


# --- slack_views.parse_week_submission ---------------------------------------

def _view_with(day_values: dict, week_start="2026-07-27", user_name="Test User") -> dict:
    """Build a minimal view_submission-shaped payload. day_values: {offset: (location, text)}.
    Only includes the relevant client block(s) when the real modal would actually
    render them -- accurately simulating what Slack sends, since a block simply
    isn't present in state when it isn't currently shown.

    For "Client Office", `text` (if not None) simulates picking that value
    directly from the dropdown (client_select_N's selected_option) -- for the
    "type a custom name" flow, use _view_with_custom_client instead."""
    values = {}
    for offset in range(5):
        loc, text = day_values.get(offset, (None, None))
        values[f"day_{offset}"] = {
            "location": {"selected_option": {"value": loc} if loc else None}
        }
        if loc == "Client Office":
            values[f"client_select_{offset}"] = {
                slack_views.CLIENT_SELECT_ACTION_ID: {"selected_option": {"value": text} if text else None}
            }
        elif loc == "Other":
            values[f"client_{offset}"] = {"text": {"value": text}}
    return {
        "private_metadata": json.dumps({"week_start": week_start, "user_name": user_name}),
        "state": {"values": values},
    }


def _view_with_custom_client(offset: int, custom_text: str | None, week_start="2026-07-27", user_name="Test User") -> dict:
    """Simulates a Client Office day where "Other (type below)" was chosen in the
    dropdown, with custom_text typed into the resulting client_custom_N block."""
    values = {
        f"day_{offset}": {"location": {"selected_option": {"value": "Client Office"}}},
        f"client_select_{offset}": {
            slack_views.CLIENT_SELECT_ACTION_ID: {"selected_option": {"value": slack_views.CUSTOM_CLIENT_VALUE}}
        },
        f"client_custom_{offset}": {"text": {"value": custom_text}},
    }
    return {
        "private_metadata": json.dumps({"week_start": week_start, "user_name": user_name}),
        "state": {"values": values},
    }


def test_parse_week_submission_all_days_filled():
    view = _view_with({
        0: ("Neal Street", None),
        1: ("WFH", None),
        2: ("Client Office", "Acme Corp"),
        3: ("Holiday", None),
        4: ("Other", "Conference"),
    })
    entries, errors = slack_views.parse_week_submission(view)

    assert errors == {}
    assert len(entries) == 5
    assert entries[0].date == "2026-07-27" and entries[0].location == "Neal Street"
    assert entries[2].location == "Client Office" and entries[2].client == "Acme Corp"
    assert entries[4].location == "Other" and entries[4].client == "Conference"


def test_parse_week_submission_some_days_blank():
    view = _view_with({0: ("Neal Street", None), 2: ("WFH", None)})
    entries, errors = slack_views.parse_week_submission(view)

    assert errors == {}
    assert len(entries) == 2
    assert {e.date for e in entries} == {"2026-07-27", "2026-07-29"}


def test_parse_week_submission_client_office_blank_client_errors():
    """Nothing picked in the dropdown at all -- error anchors to the select block."""
    view = _view_with({0: ("Client Office", None)})
    entries, errors = slack_views.parse_week_submission(view)

    assert entries == []
    assert "client_select_0" in errors


def test_parse_week_submission_client_office_custom_blank_errors():
    """"Other (type below)" chosen but left blank -- error anchors to the custom
    text block, not the select (which does have a value)."""
    view = _view_with_custom_client(0, custom_text=None)
    entries, errors = slack_views.parse_week_submission(view)

    assert entries == []
    assert "client_custom_0" in errors


def test_parse_week_submission_client_office_custom_name():
    view = _view_with_custom_client(0, custom_text="Acme Ventures")
    entries, errors = slack_views.parse_week_submission(view)

    assert errors == {}
    assert entries[0].location == "Client Office"
    assert entries[0].client == "Acme Ventures"


def test_parse_week_submission_non_client_location_has_no_text_capture():
    """A WFH/Neal Street/etc. day never renders a text block (the whole point of
    the conditional field), so there's no way to capture notes for it via Slack
    -- confirms this degrades to "no notes" rather than erroring."""
    view = _view_with({0: ("WFH", "this text is never actually reachable via the real modal")})
    entries, errors = slack_views.parse_week_submission(view)

    assert errors == {}
    assert entries[0].client is None
    assert entries[0].notes is None


def _view_with_split_day(offset: int, morning_loc: str | None, afternoon_loc: str | None, week_start="2026-07-27") -> dict:
    """Simulates a day with its "Split into morning/afternoon" checkbox
    checked, each half independently set to a plain (non-Client-Office/Other)
    location -- the common split-day case."""
    values = {
        f"day_{offset}_split": {slack_views.SPLIT_DAY_ACTION_ID: {"selected_options": [slack_views._SPLIT_DAY_CHECKBOX_OPTION]}},
        f"day_{offset}_morning": {"location": {"selected_option": {"value": morning_loc} if morning_loc else None}},
        f"day_{offset}_afternoon": {"location": {"selected_option": {"value": afternoon_loc} if afternoon_loc else None}},
    }
    return {
        "private_metadata": json.dumps({"week_start": week_start, "user_name": "Test User"}),
        "state": {"values": values},
    }


def test_parse_week_submission_split_day_produces_two_entries_with_time_periods():
    view = _view_with_split_day(0, "Neal Street", "WFH")
    entries, errors = slack_views.parse_week_submission(view)

    assert errors == {}
    assert len(entries) == 2
    by_period = {e.time_period: e for e in entries}
    assert by_period["Morning"].location == "Neal Street"
    assert by_period["Afternoon"].location == "WFH"
    assert all(e.date == "2026-07-27" for e in entries)


def test_parse_week_submission_split_day_client_office_requires_client_independently():
    """Each half of a split day validates its own Client Office/Other
    requirement independently -- morning can be valid while afternoon errors."""
    values = {
        "day_0_split": {slack_views.SPLIT_DAY_ACTION_ID: {"selected_options": [slack_views._SPLIT_DAY_CHECKBOX_OPTION]}},
        "day_0_morning": {"location": {"selected_option": {"value": "Neal Street"}}},
        "day_0_afternoon": {"location": {"selected_option": {"value": "Client Office"}}},
        "client_select_0_afternoon": {slack_views.CLIENT_SELECT_ACTION_ID: {"selected_option": None}},
    }
    view = {
        "private_metadata": json.dumps({"week_start": "2026-07-27", "user_name": "Test User"}),
        "state": {"values": values},
    }
    entries, errors = slack_views.parse_week_submission(view)

    assert len(entries) == 1
    assert entries[0].location == "Neal Street"
    assert "client_select_0_afternoon" in errors


def test_parse_week_submission_unchecked_split_ignores_leftover_half_day_values():
    """If a day isn't split, only the full-day field is read -- any stale
    morning/afternoon values (e.g. from a client the user unchecked) are
    ignored, matching how the modal only ever submits currently-rendered
    blocks."""
    values = {
        "day_0": {"location": {"selected_option": {"value": "Neal Street"}}},
        "day_0_morning": {"location": {"selected_option": {"value": "WFH"}}},
    }
    view = {
        "private_metadata": json.dumps({"week_start": "2026-07-27", "user_name": "Test User"}),
        "state": {"values": values},
    }
    entries, errors = slack_views.parse_week_submission(view)

    assert errors == {}
    assert len(entries) == 1
    assert entries[0].location == "Neal Street"
    assert entries[0].time_period is None


# --- slack_views._build_day_blocks / extract_day_state (conditional client field) --

def test_build_day_blocks_client_office_shows_dropdown_not_custom_text_by_default():
    """Picking a real client directly from the dropdown shouldn't reveal the
    custom-name text block -- only "Other (type below)" should."""
    day_state = {0: {"split": False, "full": {"location": "Client Office", "client_choice": "Sky", "text": "Sky"}}}
    blocks = slack_views._build_day_blocks("2026-07-27", day_state)

    block_ids = [b["block_id"] for b in blocks]
    assert "client_select_0" in block_ids
    assert "client_custom_0" not in block_ids

    select_block = next(b for b in blocks if b["block_id"] == "client_select_0")
    option_values = [o["value"] for o in select_block["element"]["options"]]
    assert "Sky" in option_values  # a real clients.json entry
    assert slack_views.CUSTOM_CLIENT_VALUE in option_values  # the escape hatch
    assert select_block["element"]["initial_option"]["value"] == "Sky"


def test_build_day_blocks_client_office_custom_reveals_text_block():
    day_state = {
        0: {"split": False, "full": {"location": "Client Office", "client_choice": slack_views.CUSTOM_CLIENT_VALUE, "text": "Acme Ventures"}}
    }
    blocks = slack_views._build_day_blocks("2026-07-27", day_state)

    block_ids = [b["block_id"] for b in blocks]
    assert "client_select_0" in block_ids
    assert "client_custom_0" in block_ids

    custom_block = next(b for b in blocks if b["block_id"] == "client_custom_0")
    assert custom_block["element"]["initial_value"] == "Acme Ventures"


def test_build_day_blocks_other_location_shows_plain_text_not_dropdown():
    day_state = {0: {"split": False, "full": {"location": "Other", "client_choice": None, "text": "Conference"}}}
    blocks = slack_views._build_day_blocks("2026-07-27", day_state)

    block_ids = [b["block_id"] for b in blocks]
    assert "client_0" in block_ids
    assert "client_select_0" not in block_ids

    text_block = next(b for b in blocks if b["block_id"] == "client_0")
    assert text_block["element"]["type"] == "plain_text_input"
    assert text_block["element"]["initial_value"] == "Conference"


def test_build_day_blocks_omits_any_client_field_for_non_client_locations():
    day_state = {
        0: {"split": False, "full": {"location": "WFH", "client_choice": None, "text": None}},
        1: {"split": False, "full": {"location": "Neal Street", "client_choice": None, "text": None}},
    }
    blocks = slack_views._build_day_blocks("2026-07-27", day_state)

    block_ids = [b["block_id"] for b in blocks]
    assert "day_0" in block_ids and "day_1" in block_ids
    for prefix in ("client_", "client_select_", "client_custom_"):
        assert f"{prefix}0" not in block_ids and f"{prefix}1" not in block_ids


def test_build_day_blocks_split_day_shows_two_independent_location_fields():
    day_state = {
        0: {
            "split": True,
            "morning": {"location": "Neal Street", "client_choice": None, "text": None},
            "afternoon": {"location": "Client Office", "client_choice": "Sky", "text": "Sky"},
        }
    }
    blocks = slack_views._build_day_blocks("2026-07-27", day_state)
    block_ids = [b["block_id"] for b in blocks]

    assert "day_0_split" in block_ids
    assert "day_0" not in block_ids  # full-day field not rendered when split
    assert "day_0_morning" in block_ids
    assert "day_0_afternoon" in block_ids
    assert "client_select_0_afternoon" in block_ids  # afternoon's own Client Office field
    assert "client_select_0_morning" not in block_ids  # morning is Neal Street, no client field

    split_block = next(b for b in blocks if b["block_id"] == "day_0_split")
    assert split_block["element"]["initial_options"] == [slack_views._SPLIT_DAY_CHECKBOX_OPTION]

    morning_block = next(b for b in blocks if b["block_id"] == "day_0_morning")
    assert morning_block["element"]["initial_option"]["value"] == "Neal Street"

    afternoon_block = next(b for b in blocks if b["block_id"] == "day_0_afternoon")
    assert afternoon_block["element"]["initial_option"]["value"] == "Client Office"


def test_build_day_blocks_unchecked_split_defaults_to_a_blank_full_day_field():
    """A day with no state at all (not yet touched) renders as a plain,
    unchecked, blank single dropdown -- the default, most common case."""
    blocks = slack_views._build_day_blocks("2026-07-27", {})
    block_ids = [b["block_id"] for b in blocks]

    assert "day_0_split" in block_ids
    assert "day_0" in block_ids
    assert "day_0_morning" not in block_ids
    assert "day_0_afternoon" not in block_ids

    split_block = next(b for b in blocks if b["block_id"] == "day_0_split")
    assert "initial_options" not in split_block["element"]


def test_extract_day_state_handles_missing_client_block():
    """If a day's client block(s) simply aren't in values (not currently
    rendered), extract_day_state should read them as no text, not raise."""
    values = {
        "day_0": {"location": {"selected_option": {"value": "WFH"}}},
        # no client_0/client_select_0/client_custom_0 keys at all, and no
        # day_0_split -- not a split day
    }
    day_state = slack_views.extract_day_state(values)

    assert day_state[0]["split"] is False
    assert day_state[0]["full"] == {"location": "WFH", "client_choice": None, "text": None}
    assert day_state[0]["morning"] == {"location": None, "client_choice": None, "text": None}
    assert day_state[0]["afternoon"] == {"location": None, "client_choice": None, "text": None}


def test_extract_day_state_resolves_custom_client_text():
    values = {
        "day_0": {"location": {"selected_option": {"value": "Client Office"}}},
        "client_select_0": {slack_views.CLIENT_SELECT_ACTION_ID: {"selected_option": {"value": slack_views.CUSTOM_CLIENT_VALUE}}},
        "client_custom_0": {"text": {"value": "Acme Ventures"}},
    }
    day_state = slack_views.extract_day_state(values)

    assert day_state[0]["full"] == {
        "location": "Client Office", "client_choice": slack_views.CUSTOM_CLIENT_VALUE, "text": "Acme Ventures"
    }


def test_extract_day_state_reads_split_checkbox_and_both_halves():
    values = {
        "day_0_split": {slack_views.SPLIT_DAY_ACTION_ID: {"selected_options": [slack_views._SPLIT_DAY_CHECKBOX_OPTION]}},
        "day_0_morning": {"location": {"selected_option": {"value": "Neal Street"}}},
        "day_0_afternoon": {"location": {"selected_option": {"value": "WFH"}}},
    }
    day_state = slack_views.extract_day_state(values)

    assert day_state[0]["split"] is True
    assert day_state[0]["morning"]["location"] == "Neal Street"
    assert day_state[0]["afternoon"]["location"] == "WFH"
    assert day_state[0]["full"] == {"location": None, "client_choice": None, "text": None}


# --- slack_views.format_week_summary ------------------------------------------

class _Row:
    def __init__(self, date, location, user_name, client=None, time_period=None):
        self.date, self.location, self.user_name, self.client = date, location, user_name, client
        self.time_period = time_period


def test_format_location_groups_shows_neal_street_and_client_office_on_separate_lines():
    day_rows = [
        _Row("2026-07-27", "Neal Street", "Alice"),
        _Row("2026-07-27", "Client Office", "Bob", client="Sky"),
        _Row("2026-07-27", "Client Office", "Carol", client="FT"),
        _Row("2026-07-27", "WFH", "Dave"),
    ]

    text = slack_views._format_location_groups(day_rows, {})
    lines = text.split("\n")

    assert "🏢 *Neal Street (1)*" in lines
    assert "@Alice" in lines
    assert "💼 *Client Office (2)*" in lines
    assert "*Sky*: @Bob" in lines
    assert "*FT*: @Carol" in lines
    assert "Dave" not in text


def test_format_location_groups_headcount_reflects_unique_people():
    day_rows = [
        _Row("2026-07-27", "Neal Street", "Alice"),
        _Row("2026-07-27", "Neal Street", "Bob"),
        _Row("2026-07-27", "Client Office", "Carol", client="Sky"),
        _Row("2026-07-27", "Client Office", "Dave", client="FT"),
        _Row("2026-07-27", "Client Office", "Eve", client="FT"),
    ]

    text = slack_views._format_location_groups(day_rows, {})

    assert "🏢 *Neal Street (2)*" in text
    assert "💼 *Client Office (3)*" in text


def test_format_location_groups_omits_empty_sections():
    only_client_office = [_Row("2026-07-27", "Client Office", "Bob", client="Sky")]
    text = slack_views._format_location_groups(only_client_office, {})
    assert "Neal Street" not in text
    assert "*Sky*: @Bob" in text


def test_format_location_groups_shows_placeholder_when_nobody_in_the_office():
    assert slack_views._format_location_groups([], {}) == "_No one in the office_"


def test_build_neal_street_week_message_includes_neal_street_and_client_office_only():
    """Neal Street and Client Office both appear (the "who's in an office"
    question people actually ask) -- other locations (WFH, Holiday, etc.)
    don't."""
    week_entries = [
        _Row("2026-07-27", "Neal Street", "Alice"),
        _Row("2026-07-27", "Neal Street", "Bob"),
        _Row("2026-07-27", "WFH", "Carol"),
        _Row("2026-07-29", "Client Office", "Dave", client="Sky"),
    ]

    message = slack_views.build_neal_street_week_message(week_entries, "2026-07-27")
    blocks_text = json.dumps(message["blocks"])

    assert "Alice" in blocks_text and "Bob" in blocks_text
    assert "Carol" not in blocks_text
    assert "Dave" in blocks_text and "Sky" in blocks_text


def test_build_neal_street_week_message_has_a_divider_and_section_per_day():
    message = slack_views.build_neal_street_week_message([], "2026-07-27")
    blocks = message["blocks"]

    day_sections = [b for b in blocks if b["type"] == "section" and "Mon" in b.get("text", {}).get("text", "")]
    assert len(day_sections) == 1
    assert "No one in the office" in day_sections[0]["text"]["text"]

    dividers = [b for b in blocks if b["type"] == "divider"]
    assert len(dividers) == 6  # one before each of the 5 days, one before the button


def test_build_neal_street_week_message_ends_with_full_schedule_button():
    message = slack_views.build_neal_street_week_message([], "2026-07-27")
    actions_block = message["blocks"][-1]

    assert actions_block["type"] == "actions"
    button = actions_block["elements"][0]
    assert button["text"]["text"] == "See Full Schedule"
    assert button["url"] == slack_views.TRACKER_URL


def test_build_neal_street_week_message_omits_enter_week_button_by_default():
    """The post-submission "this week" summary shouldn't offer to re-enter the
    week the user just submitted -- only the next-week digest opts in."""
    message = slack_views.build_neal_street_week_message([], "2026-07-27")
    actions_block = message["blocks"][-1]

    assert len(actions_block["elements"]) == 1


def test_build_neal_street_week_message_shows_enter_week_button_when_requested():
    message = slack_views.build_neal_street_week_message([], "2026-07-27", show_enter_week_button=True)
    actions_block = message["blocks"][-1]

    assert len(actions_block["elements"]) == 2
    enter_week_button = actions_block["elements"][1]
    assert enter_week_button["text"]["text"] == "📝 Enter My Week"
    assert enter_week_button["action_id"] == slack_views.ACTION_FILL_WEEK
    assert enter_week_button["value"] == "2026-07-27"


def test_build_neal_street_week_message_mentions_matched_directory_entries():
    directory = {"alice johnson": {"id": "U001", "real_name": "Alice Johnson"}}
    week_entries = [
        _Row("2026-07-27", "Neal Street", "Alice Johnson"),
        _Row("2026-07-27", "Neal Street", "Ghost Person"),
    ]

    message = slack_views.build_neal_street_week_message(week_entries, "2026-07-27", directory)
    blocks_text = json.dumps(message["blocks"])

    assert "<@U001>" in blocks_text  # matched -- real clickable mention
    assert "@Ghost Person" in blocks_text  # unmatched -- falls back to plain text


def test_build_neal_street_week_message_shows_every_name_no_truncation():
    """Explicitly requested: no "N others" truncation, list everyone."""
    directory = {}
    names = [f"Person{i}" for i in range(8)]
    week_entries = [_Row("2026-07-27", "Neal Street", n) for n in names]

    message = slack_views.build_neal_street_week_message(week_entries, "2026-07-27", directory)
    blocks_text = json.dumps(message["blocks"])

    assert "others" not in blocks_text
    for name in names:
        assert f"@{name}" in blocks_text


def test_build_neal_street_week_message_custom_header_overrides_default():
    message = slack_views.build_neal_street_week_message([], "2026-07-27", header_text="Here's who's at Neal Street next week")

    assert message["blocks"][0]["text"]["text"] == "*Here's who's at Neal Street next week*"
    assert message["text"] == "Here's who's at Neal Street next week"


def test_build_neal_street_tomorrow_message_has_friendly_header():
    message = slack_views.build_neal_street_tomorrow_message("2026-07-29", [])
    header_text = message["blocks"][0]["text"]["text"]

    assert "Good afternoon" in header_text
    assert "in the office tomorrow" in header_text


def test_build_neal_street_tomorrow_message_shows_day_and_mentions():
    directory = {"alice johnson": {"id": "U001", "real_name": "Alice Johnson"}}
    day_rows = [
        _Row("2026-07-29", "Neal Street", "Alice Johnson"),
        _Row("2026-07-29", "Neal Street", "Ghost Person"),
    ]
    message = slack_views.build_neal_street_tomorrow_message("2026-07-29", day_rows, directory)
    blocks_text = json.dumps(message["blocks"])

    assert "Wed 29" in blocks_text
    assert "<@U001>" in blocks_text
    assert "@Ghost Person" in blocks_text


def test_build_neal_street_tomorrow_message_ends_with_full_schedule_button():
    message = slack_views.build_neal_street_tomorrow_message("2026-07-29", [])
    actions_block = message["blocks"][-1]

    assert actions_block["type"] == "actions"
    button = actions_block["elements"][0]
    assert button["text"]["text"] == "See Full Schedule"
    assert button["url"] == slack_views.TRACKER_URL


def test_build_neal_street_tomorrow_message_has_enter_my_week_button_for_its_own_week():
    # 2026-07-29 is a Wednesday -- its week starts Monday 2026-07-27
    message = slack_views.build_neal_street_tomorrow_message("2026-07-29", [])
    actions_block = message["blocks"][-1]

    enter_week_button = actions_block["elements"][1]
    assert enter_week_button["text"]["text"] == "📝 Enter My Week"
    assert enter_week_button["action_id"] == slack_views.ACTION_FILL_WEEK
    assert enter_week_button["value"] == "2026-07-27"


def test_build_neal_street_tomorrow_message_handles_a_weekend_date_without_crashing():
    """Regression test: force=True bypasses the "tomorrow is a weekend" gate
    (see run_tomorrow_digest), so this must accept a Saturday/Sunday date_str
    without raising -- WEEKDAY_NAMES used to only cover Mon-Fri, causing an
    IndexError -> 500 in production when force-triggered on a Friday."""
    saturday_message = slack_views.build_neal_street_tomorrow_message("2026-07-25", [])
    assert "Sat 25" in saturday_message["blocks"][2]["text"]["text"]

    sunday_message = slack_views.build_neal_street_tomorrow_message("2026-07-26", [])
    assert "Sun 26" in sunday_message["blocks"][2]["text"]["text"]


def test_build_neal_street_today_message_has_friendly_header():
    message = slack_views.build_neal_street_today_message("2026-07-29", [])
    header_text = message["blocks"][0]["text"]["text"]

    assert header_text == "*:coffee: Good morning everyone! Here's who's in the office today :point_down:*"


def test_build_neal_street_today_message_shows_day_and_mentions():
    directory = {"alice johnson": {"id": "U001", "real_name": "Alice Johnson"}}
    day_rows = [
        _Row("2026-07-29", "Neal Street", "Alice Johnson"),
        _Row("2026-07-29", "Neal Street", "Ghost Person"),
    ]
    message = slack_views.build_neal_street_today_message("2026-07-29", day_rows, directory)
    blocks_text = json.dumps(message["blocks"])

    assert "Wed 29" in blocks_text
    assert "<@U001>" in blocks_text
    assert "@Ghost Person" in blocks_text


def test_build_neal_street_today_message_shows_client_office_grouped_by_client():
    day_rows = [
        _Row("2026-07-29", "Neal Street", "Alice"),
        _Row("2026-07-29", "Client Office", "Bob", client="Sky"),
        _Row("2026-07-29", "Client Office", "Carol", client="FT"),
    ]
    message = slack_views.build_neal_street_today_message("2026-07-29", day_rows)
    day_text = message["blocks"][2]["text"]["text"]

    assert "Neal Street" in day_text and "@Alice" in day_text
    assert "Client Office" in day_text
    assert "*Sky*: @Bob" in day_text
    assert "*FT*: @Carol" in day_text


def test_build_neal_street_today_message_ends_with_full_schedule_button():
    message = slack_views.build_neal_street_today_message("2026-07-29", [])
    actions_block = message["blocks"][-1]

    assert actions_block["type"] == "actions"
    button = actions_block["elements"][0]
    assert button["text"]["text"] == "See Full Schedule"
    assert button["url"] == slack_views.TRACKER_URL


def test_build_neal_street_today_message_has_enter_my_week_button_for_its_own_week():
    # 2026-07-29 is a Wednesday -- its week starts Monday 2026-07-27
    message = slack_views.build_neal_street_today_message("2026-07-29", [])
    actions_block = message["blocks"][-1]

    enter_week_button = actions_block["elements"][1]
    assert enter_week_button["text"]["text"] == "📝 Enter My Week"
    assert enter_week_button["action_id"] == slack_views.ACTION_FILL_WEEK
    assert enter_week_button["value"] == "2026-07-27"


# --- slack_routes._handle_location_change (live modal update) ----------------

def test_handle_location_change_shows_client_field_and_preserves_other_days(monkeypatch):
    captured = {}
    monkeypatch.setattr(slack_client, "update_view", lambda view_id, view_hash, view: captured.update(
        view_id=view_id, view_hash=view_hash, view=view
    ))

    # Simulate: day 0 already had WFH set (no client block), day 1 just got
    # changed to "Client Office" with no text yet -- the resulting rebuilt view
    # should show day 1's client block and still remember day 0's location.
    payload = {
        "view": {
            "id": "V123",
            "hash": "hash123",
            "private_metadata": json.dumps({"week_start": "2026-07-27", "user_name": "Test User"}),
            "state": {
                "values": {
                    "day_0": {"location": {"selected_option": {"value": "WFH"}}},
                    "day_1": {"location": {"selected_option": {"value": "Client Office"}}},
                }
            },
        }
    }

    response = slack_routes._handle_location_change(payload)

    assert response.status_code == 200
    assert captured["view_id"] == "V123"
    assert captured["view_hash"] == "hash123"

    block_ids = [b["block_id"] for b in captured["view"]["blocks"]]
    assert "client_select_0" not in block_ids  # WFH -- no client field
    assert "client_select_1" in block_ids       # Client Office -- dropdown shown

    day_0_block = next(b for b in captured["view"]["blocks"] if b["block_id"] == "day_0")
    assert day_0_block["element"]["initial_option"]["value"] == "WFH"  # preserved


# --- slack_routes._handle_block_action ACTION_SAME_AS_LAST_WEEK --------------

class _FullEntry:
    def __init__(self, location, client=None, notes=None):
        self.location, self.client, self.notes = location, client, notes


def test_same_as_last_week_opens_prefilled_confirmation_modal_instead_of_saving(monkeypatch):
    """The whole point of this change: clicking "Same as last week" should let
    the user review before it's saved, not save blind."""
    monkeypatch.setattr(slack_routes, "_resolve_identity", lambda user_id: ("Alice Johnson", True))
    monkeypatch.setattr(
        slack_routes.queries,
        "get_last_week_entries_for_user",
        lambda session, user_key, week_start: {
            0: {"full": _FullEntry("Neal Street"), "morning": None, "afternoon": None},
            1: {"full": _FullEntry("WFH"), "morning": None, "afternoon": None},
        },
    )

    saved = {"called": False}
    monkeypatch.setattr(
        slack_routes.entries_module, "upsert_entries", lambda *a, **kw: saved.update(called=True)
    )

    captured = {}
    monkeypatch.setattr(
        slack_client, "open_view", lambda trigger_id, view: captured.update(trigger_id=trigger_id, view=view)
    )

    payload = {
        "actions": [{"action_id": slack_views.ACTION_SAME_AS_LAST_WEEK, "value": "2026-07-27"}],
        "user": {"id": "U1"},
        "trigger_id": "T123",
        "response_url": "https://hooks.slack.com/fake",
    }

    response = slack_routes._handle_block_action(session=None, payload=payload)

    assert response.status_code == 200
    assert saved["called"] is False  # nothing saved yet -- only the modal opened
    assert captured["trigger_id"] == "T123"
    assert captured["view"]["title"]["text"] == "Same as last week"

    day_0_block = next(b for b in captured["view"]["blocks"] if b.get("block_id") == "day_0")
    assert day_0_block["element"]["initial_option"]["value"] == "Neal Street"
    day_1_block = next(b for b in captured["view"]["blocks"] if b.get("block_id") == "day_1")
    assert day_1_block["element"]["initial_option"]["value"] == "WFH"


def test_same_as_last_week_carries_over_a_split_day_in_the_modal(monkeypatch):
    """Split days are fully representable in the modal now, so "Same as last
    week" should pre-fill them (checkbox pre-checked, both halves filled in),
    not skip them."""
    monkeypatch.setattr(slack_routes, "_resolve_identity", lambda user_id: ("Alice Johnson", True))
    monkeypatch.setattr(
        slack_routes.queries,
        "get_last_week_entries_for_user",
        lambda session, user_key, week_start: {
            0: {"full": _FullEntry("Neal Street"), "morning": None, "afternoon": None},
            1: {"full": None, "morning": _FullEntry("Neal Street"), "afternoon": _FullEntry("WFH")},
        },
    )

    captured = {}
    monkeypatch.setattr(
        slack_client, "open_view", lambda trigger_id, view: captured.update(view=view)
    )

    payload = {
        "actions": [{"action_id": slack_views.ACTION_SAME_AS_LAST_WEEK, "value": "2026-07-27"}],
        "user": {"id": "U1"},
        "trigger_id": "T123",
        "response_url": "https://hooks.slack.com/fake",
    }

    slack_routes._handle_block_action(session=None, payload=payload)

    blocks = captured["view"]["blocks"]
    block_ids = [b.get("block_id") for b in blocks]

    day_0_block = next(b for b in blocks if b.get("block_id") == "day_0")
    assert day_0_block["element"]["initial_option"]["value"] == "Neal Street"

    assert "day_1_split" in block_ids
    day_1_split_block = next(b for b in blocks if b.get("block_id") == "day_1_split")
    assert day_1_split_block["element"]["initial_options"] == [slack_views._SPLIT_DAY_CHECKBOX_OPTION]

    day_1_morning_block = next(b for b in blocks if b.get("block_id") == "day_1_morning")
    assert day_1_morning_block["element"]["initial_option"]["value"] == "Neal Street"

    day_1_afternoon_block = next(b for b in blocks if b.get("block_id") == "day_1_afternoon")
    assert day_1_afternoon_block["element"]["initial_option"]["value"] == "WFH"


def test_same_as_last_week_with_no_full_day_entries_responds_without_opening_modal(monkeypatch):
    monkeypatch.setattr(slack_routes, "_resolve_identity", lambda user_id: ("Alice Johnson", True))
    monkeypatch.setattr(
        slack_routes.queries, "get_last_week_entries_for_user", lambda session, user_key, week_start: {}
    )

    open_view_called = {"called": False}
    monkeypatch.setattr(
        slack_client, "open_view", lambda trigger_id, view: open_view_called.update(called=True)
    )

    responded = {}
    monkeypatch.setattr(
        slack_client, "respond_via_response_url", lambda url, text, **kw: responded.update(text=text)
    )

    payload = {
        "actions": [{"action_id": slack_views.ACTION_SAME_AS_LAST_WEEK, "value": "2026-07-27"}],
        "user": {"id": "U1"},
        "trigger_id": "T123",
        "response_url": "https://hooks.slack.com/fake",
    }

    response = slack_routes._handle_block_action(session=None, payload=payload)

    assert response.status_code == 200
    assert open_view_called["called"] is False
    assert "Fill in week" in responded["text"]


def test_handle_block_action_url_button_does_not_crash():
    """Regression test: Slack sends a block_actions payload to our Request URL
    even for a "url"-only button (it also opens the link client-side) -- this
    caused a KeyError -> 500 when the button had no action_id/value set."""
    payload = {
        "actions": [{"action_id": slack_views.ACTION_VIEW_FULL_SCHEDULE, "value": slack_views.TRACKER_URL}],
        "user": {"id": "U1"},
        "response_url": "https://hooks.slack.com/fake",
    }

    response = slack_routes._handle_block_action(session=None, payload=payload)

    assert response.status_code == 200


def test_handle_block_action_unknown_action_id_does_not_crash():
    """Defensive: any future/unexpected action_id (missing "value", etc.)
    should ack cleanly rather than raising."""
    payload = {"actions": [{"action_id": "something_we_dont_recognize"}], "user": {"id": "U1"}}

    response = slack_routes._handle_block_action(session=None, payload=payload)

    assert response.status_code == 200


# --- daily_notifications DST-safe hour/weekday gate --------------------------

class _FixedDatetime(datetime):
    """A datetime subclass whose .now() always returns a fixed instant, so the
    gate logic can be tested without depending on the real wall clock."""
    _fixed = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


class _FakeSession:
    """A minimal stand-in for a real DB session, just enough for the
    ScheduledRunLog dedup check/mark in daily_notifications.py's gates: no
    prior run recorded, and add/commit are no-ops. Used by gate tests that
    don't otherwise touch the database."""
    def get(self, model, pk):
        return None

    def add(self, obj):
        pass

    def commit(self):
        pass


class _RecordingFakeSession:
    """Like _FakeSession, but actually remembers what _mark_ran_today records
    (keyed by ScheduledRunLog.job_name), so a second call within the same test
    can observe "already sent today" -- exercises the dedup path for real."""
    def __init__(self):
        self._rows = {}

    def get(self, model, pk):
        return self._rows.get(pk)

    def add(self, obj):
        self._rows[obj.job_name] = obj

    def commit(self):
        pass


def _run_today_digest_with_fixed_now(monkeypatch, fixed_dt):
    fixed = _FixedDatetime(
        fixed_dt.year, fixed_dt.month, fixed_dt.day,
        fixed_dt.hour, fixed_dt.minute, tzinfo=fixed_dt.tzinfo,
    )
    _FixedDatetime._fixed = fixed
    monkeypatch.setattr(daily_notifications, "datetime", _FixedDatetime)
    return daily_notifications.run_today_digest(_FakeSession())


def test_today_digest_gate_skips_weekend(monkeypatch):
    saturday_9am = datetime(2026, 7, 25, 9, 0, tzinfo=ZoneInfo("Europe/London"))  # a Saturday
    result = _run_today_digest_with_fixed_now(monkeypatch, saturday_9am)
    assert result == {"ok": True, "skipped": "weekend"}


def test_today_digest_gate_skips_off_hour(monkeypatch):
    """Before the target hour is still rejected regardless of GATE_WINDOW_HOURS
    -- the widened window only extends how late a firing can be, never how early."""
    tuesday_3am = datetime(2026, 7, 28, 3, 0, tzinfo=ZoneInfo("Europe/London"))
    result = _run_today_digest_with_fixed_now(monkeypatch, tuesday_3am)
    assert result["skipped"] == "not target hour"
    assert result["hour"] == 3


def test_today_digest_gate_matches_target_hour_across_dst(monkeypatch):
    """Both a BST morning and a GMT-era morning at the configured target hour
    should pass the gate (not be skipped) -- this is the whole point of firing
    the GitHub Actions cron at both UTC 08:00 and 09:00."""
    for tz_date in [
        datetime(2026, 7, 28, 9, 0, tzinfo=ZoneInfo("Europe/London")),   # BST (summer)
        datetime(2026, 1, 27, 9, 0, tzinfo=ZoneInfo("Europe/London")),  # GMT (winter)
    ]:
        fixed = _FixedDatetime(
            tz_date.year, tz_date.month, tz_date.day, tz_date.hour, tz_date.minute, tzinfo=tz_date.tzinfo
        )
        _FixedDatetime._fixed = fixed
        monkeypatch.setattr(daily_notifications, "datetime", _FixedDatetime)

        # SLACK_GENERAL_CHANNEL_ID unset in test env -> digest step no-ops
        # safely. We're only asserting the gate itself didn't short-circuit.
        result = daily_notifications.run_today_digest(_FakeSession())
        assert "skipped" not in result
        assert result["ok"] is True


def test_today_digest_gate_accepts_a_late_firing_within_the_window(monkeypatch):
    """Regression test: GitHub Actions' scheduler has been observed firing a
    9am-targeted cron hours late -- a firing within GATE_WINDOW_HOURS of the
    target should still send, not silently no-op."""
    tuesday_2pm = datetime(2026, 7, 28, 14, 0, tzinfo=ZoneInfo("Europe/London"))  # 5 hours after 9am
    result = _run_today_digest_with_fixed_now(monkeypatch, tuesday_2pm)
    assert "skipped" not in result


def test_today_digest_gate_rejects_a_firing_beyond_the_window(monkeypatch):
    tuesday_9pm = datetime(2026, 7, 28, 21, 0, tzinfo=ZoneInfo("Europe/London"))  # 12 hours after 9am
    result = _run_today_digest_with_fixed_now(monkeypatch, tuesday_9pm)
    assert result["skipped"] == "not target hour"
    assert result["hour"] == 21


def test_today_digest_gate_skips_if_already_sent_today(monkeypatch):
    """The ScheduledRunLog dedup check: a second gate pass on the same day
    (e.g. the redundant BST/GMT cron firing, or a delayed retry) must not send
    again, even though the hour is still within the widened window."""
    tuesday_9am = datetime(2026, 7, 28, 9, 0, tzinfo=ZoneInfo("Europe/London"))
    fixed = _FixedDatetime(
        tuesday_9am.year, tuesday_9am.month, tuesday_9am.day,
        tuesday_9am.hour, tuesday_9am.minute, tzinfo=tuesday_9am.tzinfo,
    )
    _FixedDatetime._fixed = fixed
    monkeypatch.setattr(daily_notifications, "datetime", _FixedDatetime)

    session = _RecordingFakeSession()
    first = daily_notifications.run_today_digest(session)
    assert "skipped" not in first

    second = daily_notifications.run_today_digest(session)
    assert second == {"ok": True, "skipped": "already sent today"}


def test_scheduled_run_log_dedup_is_isolated_per_job(monkeypatch):
    """Sending today's digest must not block that same day's unfilled
    reminders (or vice versa) -- each job's ScheduledRunLog row is keyed by
    its own job_name."""
    tuesday_9am = datetime(2026, 7, 28, 9, 0, tzinfo=ZoneInfo("Europe/London"))
    fixed = _FixedDatetime(
        tuesday_9am.year, tuesday_9am.month, tuesday_9am.day,
        tuesday_9am.hour, tuesday_9am.minute, tzinfo=tuesday_9am.tzinfo,
    )
    _FixedDatetime._fixed = fixed
    monkeypatch.setattr(daily_notifications, "datetime", _FixedDatetime)
    monkeypatch.setattr(daily_notifications.queries, "get_submitted_users", lambda session, week_start: [])
    monkeypatch.setattr(daily_notifications.roster, "get_roster", lambda: [])

    session = _RecordingFakeSession()
    digest_result = daily_notifications.run_today_digest(session)
    reminders_result = daily_notifications.run_unfilled_reminders(session)

    assert "skipped" not in digest_result
    assert "skipped" not in reminders_result


def test_today_digest_force_bypasses_gate(monkeypatch):
    saturday_9am = datetime(2026, 7, 25, 9, 0, tzinfo=ZoneInfo("Europe/London"))  # a Saturday
    fixed = _FixedDatetime(
        saturday_9am.year, saturday_9am.month, saturday_9am.day,
        saturday_9am.hour, saturday_9am.minute, tzinfo=saturday_9am.tzinfo,
    )
    _FixedDatetime._fixed = fixed
    monkeypatch.setattr(daily_notifications, "datetime", _FixedDatetime)
    result = daily_notifications.run_today_digest(session=None, force=True)
    assert "skipped" not in result
    assert result["ok"] is True


# --- daily_notifications unfilled-reminders gate ------------------------------

def _run_unfilled_reminders_with_fixed_now(monkeypatch, fixed_dt):
    fixed = _FixedDatetime(
        fixed_dt.year, fixed_dt.month, fixed_dt.day,
        fixed_dt.hour, fixed_dt.minute, tzinfo=fixed_dt.tzinfo,
    )
    _FixedDatetime._fixed = fixed
    monkeypatch.setattr(daily_notifications, "datetime", _FixedDatetime)
    return daily_notifications.run_unfilled_reminders(_FakeSession())


def test_unfilled_reminders_gate_skips_weekend(monkeypatch):
    saturday_9am = datetime(2026, 7, 25, 9, 0, tzinfo=ZoneInfo("Europe/London"))  # a Saturday
    result = _run_unfilled_reminders_with_fixed_now(monkeypatch, saturday_9am)
    assert result == {"ok": True, "skipped": "weekend"}


def test_unfilled_reminders_gate_skips_off_hour(monkeypatch):
    """Before the target hour is still rejected regardless of GATE_WINDOW_HOURS
    -- the widened window only extends how late a firing can be, never how early."""
    tuesday_3am = datetime(2026, 7, 28, 3, 0, tzinfo=ZoneInfo("Europe/London"))
    result = _run_unfilled_reminders_with_fixed_now(monkeypatch, tuesday_3am)
    assert result["skipped"] == "not target hour"
    assert result["hour"] == 3


def test_unfilled_reminders_gate_matches_target_hour_across_dst(monkeypatch):
    """Both a BST morning and a GMT-era morning at the configured target hour
    should pass the gate (not be skipped) -- this is the whole point of firing
    the GitHub Actions cron at both UTC 08:00 and 09:00."""
    for tz_date in [
        datetime(2026, 7, 28, 9, 0, tzinfo=ZoneInfo("Europe/London")),   # BST (summer)
        datetime(2026, 1, 27, 9, 0, tzinfo=ZoneInfo("Europe/London")),  # GMT (winter)
    ]:
        monkeypatch.setattr(daily_notifications.roster, "get_roster", lambda: [])
        fixed = _FixedDatetime(
            tz_date.year, tz_date.month, tz_date.day, tz_date.hour, tz_date.minute, tzinfo=tz_date.tzinfo
        )
        _FixedDatetime._fixed = fixed
        monkeypatch.setattr(daily_notifications, "datetime", _FixedDatetime)

        # roster empty -> reminder step no-ops safely. We're only asserting
        # the gate itself didn't short-circuit with a "skipped" result.
        monkeypatch.setattr(daily_notifications.queries, "get_submitted_users", lambda session, week_start: [])

        result = daily_notifications.run_unfilled_reminders(_FakeSession())
        assert "skipped" not in result
        assert result["ok"] is True


def test_unfilled_reminders_force_bypasses_gate(monkeypatch):
    monkeypatch.setattr(daily_notifications.roster, "get_roster", lambda: [])
    monkeypatch.setattr(daily_notifications.queries, "get_submitted_users", lambda session, week_start: [])
    saturday_9am = datetime(2026, 7, 25, 9, 0, tzinfo=ZoneInfo("Europe/London"))  # a Saturday
    fixed = _FixedDatetime(
        saturday_9am.year, saturday_9am.month, saturday_9am.day,
        saturday_9am.hour, saturday_9am.minute, tzinfo=saturday_9am.tzinfo,
    )
    _FixedDatetime._fixed = fixed
    monkeypatch.setattr(daily_notifications, "datetime", _FixedDatetime)
    result = daily_notifications.run_unfilled_reminders(session=None, force=True)
    assert "skipped" not in result
    assert result["ok"] is True


# --- daily_notifications tomorrow-digest gate ---------------------------------

def _run_tomorrow_digest_with_fixed_now(monkeypatch, fixed_dt):
    fixed = _FixedDatetime(
        fixed_dt.year, fixed_dt.month, fixed_dt.day,
        fixed_dt.hour, fixed_dt.minute, tzinfo=fixed_dt.tzinfo,
    )
    _FixedDatetime._fixed = fixed
    monkeypatch.setattr(daily_notifications, "datetime", _FixedDatetime)
    return daily_notifications.run_tomorrow_digest(_FakeSession())


def test_tomorrow_digest_gate_skips_weekend(monkeypatch):
    saturday_4pm = datetime(2026, 7, 25, 16, 0, tzinfo=ZoneInfo("Europe/London"))  # a Saturday
    result = _run_tomorrow_digest_with_fixed_now(monkeypatch, saturday_4pm)
    assert result == {"ok": True, "skipped": "weekend"}


def test_tomorrow_digest_gate_skips_off_hour(monkeypatch):
    tuesday_2pm = datetime(2026, 7, 28, 14, 0, tzinfo=ZoneInfo("Europe/London"))
    result = _run_tomorrow_digest_with_fixed_now(monkeypatch, tuesday_2pm)
    assert result["skipped"] == "not target hour"
    assert result["hour"] == 14


def test_tomorrow_digest_posts_next_week_on_friday_instead_of_skipping(monkeypatch):
    """Friday's "tomorrow" would be Saturday (not useful) -- instead of
    skipping, this posts the whole of next week's schedule as one action."""
    monkeypatch.setattr(daily_notifications.queries, "get_week_entries", lambda session, week_start: [])
    friday_4pm = datetime(2026, 7, 31, 16, 0, tzinfo=ZoneInfo("Europe/London"))  # a Friday
    result = _run_tomorrow_digest_with_fixed_now(monkeypatch, friday_4pm)
    assert "skipped" not in result
    assert result["ok"] is True
    assert result["period"] == "next_week"


def test_tomorrow_digest_gate_passes_on_a_weekday_afternoon(monkeypatch):
    tuesday_4pm = datetime(2026, 7, 28, 16, 0, tzinfo=ZoneInfo("Europe/London"))  # a Tuesday
    result = _run_tomorrow_digest_with_fixed_now(monkeypatch, tuesday_4pm)
    assert "skipped" not in result
    assert result["ok"] is True
    assert result["period"] == "tomorrow"


def test_tomorrow_digest_force_bypasses_gate(monkeypatch):
    saturday_9am = datetime(2026, 7, 25, 9, 0, tzinfo=ZoneInfo("Europe/London"))  # a Saturday
    fixed = _FixedDatetime(
        saturday_9am.year, saturday_9am.month, saturday_9am.day,
        saturday_9am.hour, saturday_9am.minute, tzinfo=saturday_9am.tzinfo,
    )
    _FixedDatetime._fixed = fixed
    monkeypatch.setattr(daily_notifications, "datetime", _FixedDatetime)
    result = daily_notifications.run_tomorrow_digest(session=None, force=True)
    assert "skipped" not in result
    assert result["ok"] is True


def test_post_neal_street_next_week_digest_uses_next_week_header(monkeypatch):
    monkeypatch.setattr(daily_notifications, "_resolve_digest_channel", lambda directory: "C0GENERAL")
    monkeypatch.setattr(daily_notifications.slack_directory, "build_directory", lambda: {})
    monkeypatch.setattr(
        daily_notifications.queries,
        "get_week_entries",
        lambda session, week_start: [_Row("2026-08-03", "Neal Street", "Alice")],
    )

    captured = {}

    def _fake_post_message(channel, text, blocks=None):
        captured["channel"] = channel
        captured["text"] = text
        captured["blocks"] = blocks

    monkeypatch.setattr(daily_notifications.slack_client, "post_message", _fake_post_message)

    count = daily_notifications._post_neal_street_next_week_digest(session=None, next_week_start="2026-08-03")

    assert count == 1
    assert captured["channel"] == "C0GENERAL"
    assert captured["blocks"][0]["text"]["text"] == (
        "*:wave: Good afternoon everyone! Here's who will be in the office next week :point_down:*"
    )


# --- daily_notifications next-week-reminder gate ------------------------------

def _run_next_week_reminder_with_fixed_now(monkeypatch, fixed_dt):
    fixed = _FixedDatetime(
        fixed_dt.year, fixed_dt.month, fixed_dt.day,
        fixed_dt.hour, fixed_dt.minute, tzinfo=fixed_dt.tzinfo,
    )
    _FixedDatetime._fixed = fixed
    monkeypatch.setattr(daily_notifications, "datetime", _FixedDatetime)
    return daily_notifications.run_next_week_reminder(_FakeSession())


def test_next_week_reminder_gate_skips_non_friday(monkeypatch):
    tuesday_2pm = datetime(2026, 7, 28, 14, 0, tzinfo=ZoneInfo("Europe/London"))
    result = _run_next_week_reminder_with_fixed_now(monkeypatch, tuesday_2pm)
    assert result == {"ok": True, "skipped": "not friday"}


def test_next_week_reminder_gate_skips_off_hour(monkeypatch):
    friday_9am = datetime(2026, 7, 31, 9, 0, tzinfo=ZoneInfo("Europe/London"))  # a Friday
    result = _run_next_week_reminder_with_fixed_now(monkeypatch, friday_9am)
    assert result["skipped"] == "not target hour"
    assert result["hour"] == 9


def test_next_week_reminder_gate_passes_on_friday_afternoon(monkeypatch):
    monkeypatch.setattr(daily_notifications.roster, "get_roster", lambda: [])
    monkeypatch.setattr(daily_notifications.queries, "get_submitted_users", lambda session, week_start: [])
    friday_2pm = datetime(2026, 7, 31, 14, 0, tzinfo=ZoneInfo("Europe/London"))  # a Friday
    result = _run_next_week_reminder_with_fixed_now(monkeypatch, friday_2pm)
    assert "skipped" not in result
    assert result["ok"] is True


def test_next_week_reminder_force_bypasses_gate(monkeypatch):
    monkeypatch.setattr(daily_notifications.roster, "get_roster", lambda: [])
    monkeypatch.setattr(daily_notifications.queries, "get_submitted_users", lambda session, week_start: [])
    tuesday_2pm = datetime(2026, 7, 28, 14, 0, tzinfo=ZoneInfo("Europe/London"))
    fixed = _FixedDatetime(
        tuesday_2pm.year, tuesday_2pm.month, tuesday_2pm.day,
        tuesday_2pm.hour, tuesday_2pm.minute, tzinfo=tuesday_2pm.tzinfo,
    )
    _FixedDatetime._fixed = fixed
    monkeypatch.setattr(daily_notifications, "datetime", _FixedDatetime)
    result = daily_notifications.run_next_week_reminder(session=None, force=True)
    assert "skipped" not in result
    assert result["ok"] is True


def test_next_week_reminder_targets_the_monday_after_next(monkeypatch):
    monkeypatch.setattr(daily_notifications.roster, "get_roster", lambda: ["Alice"])
    monkeypatch.setattr(daily_notifications.queries, "get_submitted_users", lambda session, week_start: [])
    captured = {}

    def _fake_send(session, week_start, header_text=None):
        captured["week_start"] = week_start
        captured["header_text"] = header_text
        return 0, []

    monkeypatch.setattr(daily_notifications, "_send_quickfill_reminders", _fake_send)
    friday_2pm = datetime(2026, 7, 31, 14, 0, tzinfo=ZoneInfo("Europe/London"))  # a Friday
    _run_next_week_reminder_with_fixed_now(monkeypatch, friday_2pm)

    assert captured["week_start"] == "2026-08-03"  # the Monday after next
    assert "next week" in captured["header_text"]


# --- daily_notifications test-mode restriction --------------------------------

def test_resolve_digest_channel_returns_real_channel_when_test_mode_off(monkeypatch):
    monkeypatch.setattr(daily_notifications, "TEST_MODE_USER_NAME", None)
    monkeypatch.setattr(daily_notifications, "SLACK_GENERAL_CHANNEL_ID", "C0REAL")
    assert daily_notifications._resolve_digest_channel() == "C0REAL"


def test_resolve_digest_channel_redirects_to_test_user_dm_when_test_mode_on(monkeypatch):
    monkeypatch.setattr(daily_notifications, "TEST_MODE_USER_NAME", "Cam Doherty")
    monkeypatch.setattr(daily_notifications, "SLACK_GENERAL_CHANNEL_ID", "C0REAL")
    directory = {"cam doherty": {"id": "U0CAM", "real_name": "Cam Doherty"}}
    monkeypatch.setattr(daily_notifications.slack_client, "open_dm", lambda slack_id: f"DM-{slack_id}")

    channel = daily_notifications._resolve_digest_channel(directory)

    assert channel == "DM-U0CAM"


def test_resolve_digest_channel_returns_none_when_test_user_not_in_directory(monkeypatch):
    monkeypatch.setattr(daily_notifications, "TEST_MODE_USER_NAME", "Cam Doherty")
    assert daily_notifications._resolve_digest_channel({}) is None


def test_restrict_to_test_mode_filters_to_just_that_person(monkeypatch):
    monkeypatch.setattr(daily_notifications, "TEST_MODE_USER_NAME", "Cam Doherty")
    matched = {"Cam Doherty": "U0CAM", "Alice Johnson": "U0ALICE"}

    restricted = daily_notifications._restrict_to_test_mode(matched)

    assert restricted == {"Cam Doherty": "U0CAM"}


def test_restrict_to_test_mode_is_a_noop_when_test_mode_off(monkeypatch):
    monkeypatch.setattr(daily_notifications, "TEST_MODE_USER_NAME", None)
    matched = {"Cam Doherty": "U0CAM", "Alice Johnson": "U0ALICE"}

    assert daily_notifications._restrict_to_test_mode(matched) == matched


def test_build_quickfill_message_custom_header_overrides_default_and_fallback_text():
    message = slack_views.build_quickfill_message("2026-08-03", header_text="Plan next week!")

    assert message["blocks"][0]["text"]["text"].startswith("*Plan next week!*")
    assert message["text"] == "Plan next week!"


def test_build_quickfill_message_mention_is_prefixed_to_header_and_fallback():
    message = slack_views.build_quickfill_message("2026-08-03", mention="<@U0CAM>")

    header_text = message["blocks"][0]["text"]["text"]
    assert header_text.startswith("*Hey <@U0CAM> — Don't forget to fill in your week!*")
    assert message["text"].startswith("Hey <@U0CAM> — ")


def test_send_quickfill_reminders_includes_recipient_mention(monkeypatch):
    monkeypatch.setattr(daily_notifications.roster, "get_roster", lambda: ["Alice Johnson"])
    monkeypatch.setattr(daily_notifications.queries, "get_submitted_users", lambda session, week_start: [])
    monkeypatch.setattr(
        daily_notifications.queries, "get_last_week_entries_for_user", lambda session, user_key, week_start: {}
    )
    monkeypatch.setattr(
        daily_notifications.slack_directory,
        "build_directory",
        lambda: {"alice johnson": {"id": "U0ALICE", "real_name": "Alice Johnson"}},
    )
    monkeypatch.setattr(daily_notifications.slack_client, "open_dm", lambda slack_id: f"DM-{slack_id}")

    captured = {}

    def _fake_post_message(channel, text, blocks=None):
        captured["channel"] = channel
        captured["text"] = text

    monkeypatch.setattr(daily_notifications.slack_client, "post_message", _fake_post_message)

    sent, unmatched = daily_notifications._send_quickfill_reminders(session=None, week_start="2026-08-03")

    assert sent == 1
    assert captured["channel"] == "DM-U0ALICE"
    assert captured["text"].startswith("Hey <@U0ALICE> — ")
