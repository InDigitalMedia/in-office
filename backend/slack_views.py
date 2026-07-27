"""Builds and parses the Slack UI surfaces for filling in a week:

1. build_quickfill_message -- "Same as last week" / "Fill in week" buttons.
   Posted as the body of the daily unfilled-week reminder DM, which can't open a
   modal directly (no fresh trigger_id in a DM) so it needs a button first.
   /enter-week itself skips straight to the modal (see slack_routes.py) since a
   slash command already has a fresh trigger_id.
2. build_week_modal / parse_week_submission -- the day-by-day entry form. Each
   day has a "Split into morning/afternoon" checkbox; unchecked (the default)
   shows one location field, checked shows two (morning/afternoon), each
   independently able to be any location including Client Office/Other with
   their own client/description sub-field. _build_day_blocks is the single
   source of truth for which blocks exist given the current state (day split
   or not, each field's location) -- used both on initial open and on every
   live update (dispatch_action + views.update), so rendering can't drift from
   what extract_day_state/parse_week_submission expect.
3. build_neal_street_week_message -- shown privately to whoever just finished
   submitting, Officely-style: each day clearly separated, Neal Street and
   Client Office (grouped by client), with a link back to the full tracker.
"""
import json
import os
from datetime import datetime, timedelta

from pydantic import ValidationError

import clients
from schemas import EntryCreate

VALID_LOCATIONS = ["Neal Street", "WFH", "Client Office", "Holiday", "Working From Abroad", "Other"]
CLIENT_TEXT_LOCATIONS = ("Client Office", "Other")

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
# The week modal only ever covers Mon-Fri (offsets 0-4), but the single-day
# today/tomorrow digests index by date_obj.weekday() directly -- Saturday(5)/
# Sunday(6) are reachable there when force=True bypasses the weekend gate (e.g.
# manually forcing "tomorrow" on a Friday), so this list must cover all 7 days
# to avoid an IndexError -> 500.

LOCATION_ACTION_ID = "location"
CLIENT_SELECT_ACTION_ID = "client_select"
SPLIT_DAY_ACTION_ID = "split_day"
SPLIT_DAY_CHECKBOX_VALUE = "split"
# Sentinel dropdown value for "not in the list, let me type it" -- deliberately
# not a real client name so it can never collide with an actual clients.json entry.
CUSTOM_CLIENT_VALUE = "__custom__"

ACTION_SAME_AS_LAST_WEEK = "quickfill_same_as_last_week"
ACTION_FILL_WEEK = "quickfill_fill_week"

CALLBACK_ID_WEEK_MODAL = "log_week_modal"

# Any block_id whose value changing should trigger a live modal re-render.
DISPATCH_ACTION_IDS = (LOCATION_ACTION_ID, CLIENT_SELECT_ACTION_ID, SPLIT_DAY_ACTION_ID)


def _day_date(week_start: str, offset: int) -> str:
    start_date = datetime.strptime(week_start, "%Y-%m-%d").date()
    return (start_date + timedelta(days=offset)).strftime("%Y-%m-%d")


def _day_label(week_start: str, offset: int) -> str:
    date_str = _day_date(week_start, offset)
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{WEEKDAY_NAMES[offset]} {date_obj.day} {date_obj.strftime('%b')}"


def _sub_label(week_start: str, offset: int, field_name: str, half: str | None = None) -> str:
    """Label for a field nested under a specific day's location select. Slack's
    Block Kit gives every input block's label the same fixed bold styling --
    there's no way to actually indent/de-emphasize one relative to another --
    so the day association has to be carried in the text itself. The "↳" plus
    a short day reference is the closest approximation of "this is a sub-field
    of the row above" that plain-text labels can convey. half ("Morning"/
    "Afternoon") further disambiguates a client/description field on a split day,
    since both halves render the same field_name independently."""
    date_str = _day_date(week_start, offset)
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    short_day = f"{WEEKDAY_NAMES[offset][:3]} {date_obj.day}"
    if half:
        return f"↳ {field_name} ({half}, {short_day})"
    return f"↳ {field_name} ({short_day})"


def build_quickfill_message(week_start: str, header_text: str | None = None, mention: str | None = None) -> dict:
    """Block Kit message body (blocks + fallback text) for the quick-fill prompt.
    header_text lets callers reuse this for other weeks (e.g. the Friday next-week
    reminder) with wording appropriate to that context; defaults to the standard
    "fill in your week" prompt used by the same-week daily reminder. mention is a
    ready-made Slack mention string (e.g. "<@U123>") for the recipient, prefixed
    onto the header so it's clear who's being asked even if the DM is forwarded
    or screenshotted."""
    header_text = header_text or "Don't forget to fill in your week!"
    if mention:
        header_text = f"Hey {mention} — {header_text}"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{header_text}*",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔁 Same as last week", "emoji": True},
                    "action_id": ACTION_SAME_AS_LAST_WEEK,
                    "value": week_start,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✏️ Fill in week", "emoji": True},
                    "action_id": ACTION_FILL_WEEK,
                    "value": week_start,
                },
            ],
        },
    ]
    return {"text": header_text, "blocks": blocks}


def _build_location_field(label: str, block_id_suffix: str, sub_label_fn, state: dict) -> list:
    """One location select + its conditional Client Office/Other sub-fields.

    - "Other" location: a plain free-text description block (client_{suffix}).
    - "Client Office" location: a dropdown of clients.json entries plus an
      "Other (type below)" option (client_select_{suffix}); choosing that reveals
      a further custom-name text block (client_custom_{suffix}).
    - Anything else: no client-related block at all.

    block_id_suffix distinguishes this field's block_ids from any other location
    field on the same day (the day's own offset for a full day, or "{offset}_morning"/
    "{offset}_afternoon" when that day is split), so their state can never collide.
    sub_label_fn(field_name) builds this field's own sub-field labels (day-only or
    half-day-aware, depending on the caller)."""
    location = state.get("location")

    location_block = {
        "type": "input",
        "block_id": f"day_{block_id_suffix}",
        "dispatch_action": True,
        "label": {"type": "plain_text", "text": label},
        "element": {
            "type": "static_select",
            "action_id": LOCATION_ACTION_ID,
            "options": [
                {"text": {"type": "plain_text", "text": loc}, "value": loc}
                for loc in VALID_LOCATIONS
            ],
        },
    }
    if location:
        location_block["element"]["initial_option"] = {
            "text": {"type": "plain_text", "text": location},
            "value": location,
        }
    blocks = [location_block]

    if location == "Client Office":
        client_choice = state.get("client_choice")
        options = [
            {"text": {"type": "plain_text", "text": name}, "value": name}
            for name in clients.get_clients()
        ] + [{"text": {"type": "plain_text", "text": "Other (type below)"}, "value": CUSTOM_CLIENT_VALUE}]
        select_block = {
            "type": "input",
            "block_id": f"client_select_{block_id_suffix}",
            "optional": True,
            "dispatch_action": True,
            "label": {"type": "plain_text", "text": sub_label_fn("Client")},
            "element": {
                "type": "static_select",
                "action_id": CLIENT_SELECT_ACTION_ID,
                "options": options,
            },
        }
        if client_choice:
            label_text = "Other (type below)" if client_choice == CUSTOM_CLIENT_VALUE else client_choice
            select_block["element"]["initial_option"] = {
                "text": {"type": "plain_text", "text": label_text},
                "value": client_choice,
            }
        blocks.append(select_block)

        if client_choice == CUSTOM_CLIENT_VALUE:
            custom_block = {
                "type": "input",
                "block_id": f"client_custom_{block_id_suffix}",
                "optional": True,
                "label": {"type": "plain_text", "text": sub_label_fn("Client name")},
                "element": {"type": "plain_text_input", "action_id": "text"},
            }
            if state.get("text"):
                custom_block["element"]["initial_value"] = state["text"]
            blocks.append(custom_block)

    elif location == "Other":
        text_block = {
            "type": "input",
            "block_id": f"client_{block_id_suffix}",
            "optional": True,
            "label": {"type": "plain_text", "text": sub_label_fn("Description")},
            "element": {"type": "plain_text_input", "action_id": "text"},
        }
        if state.get("text"):
            text_block["element"]["initial_value"] = state["text"]
        blocks.append(text_block)

    return blocks


_SPLIT_DAY_CHECKBOX_OPTION = {
    "text": {"type": "plain_text", "text": "Split into morning/afternoon"},
    "value": SPLIT_DAY_CHECKBOX_VALUE,
}


def _build_day_blocks(week_start: str, day_state: dict) -> list:
    """day_state: {offset: {"split": bool, "full": {...}, "morning": {...}, "afternoon": {...}}},
    where each of full/morning/afternoon is {"location": str|None, "client_choice":
    str|None, "text": str|None}. Every day gets a "Split into morning/afternoon"
    checkbox; unchecked (the default) renders one location field ("full"),
    checked renders two independent ones ("morning"/"afternoon"), each with its
    own conditional Client Office/Other sub-field.

    This one function is the single source of truth for which blocks exist given
    the current state, used both when the modal first opens and every time it's
    live-updated, so rendering can't drift from what parse_week_submission expects."""
    blocks = []
    for offset in range(5):
        state = day_state.get(offset, {})
        split = bool(state.get("split"))

        if offset > 0:
            # Block Kit gives every input block the same fixed vertical gap, so
            # without this, one day's last field and the next day's date look
            # exactly as close together as two fields within the same day --
            # a divider is the only way to visually separate one day from the next.
            blocks.append({"type": "divider"})

        split_block = {
            "type": "input",
            "block_id": f"day_{offset}_split",
            "optional": True,
            "dispatch_action": True,
            "label": {"type": "plain_text", "text": _day_label(week_start, offset)},
            "element": {
                "type": "checkboxes",
                "action_id": SPLIT_DAY_ACTION_ID,
                "options": [_SPLIT_DAY_CHECKBOX_OPTION],
            },
        }
        if split:
            split_block["element"]["initial_options"] = [_SPLIT_DAY_CHECKBOX_OPTION]
        blocks.append(split_block)

        if split:
            blocks.extend(_build_location_field(
                _sub_label(week_start, offset, "Morning"),
                f"{offset}_morning",
                lambda field_name, offset=offset: _sub_label(week_start, offset, field_name, half="Morning"),
                state.get("morning", {}),
            ))
            blocks.extend(_build_location_field(
                _sub_label(week_start, offset, "Afternoon"),
                f"{offset}_afternoon",
                lambda field_name, offset=offset: _sub_label(week_start, offset, field_name, half="Afternoon"),
                state.get("afternoon", {}),
            ))
        else:
            blocks.extend(_build_location_field(
                "Location",
                str(offset),
                lambda field_name, offset=offset: _sub_label(week_start, offset, field_name),
                state.get("full", {}),
            ))

    return blocks


def _extract_location_field(values: dict, block_id_suffix: str) -> dict:
    """{"location": str|None, "client_choice": str|None, "text": str|None} for
    one location field, given the block_id suffix that identifies it (an
    offset for a full day, or "{offset}_morning"/"{offset}_afternoon" for a
    split day's halves). A block simply won't be in values if it isn't
    currently rendered -- .get(...) throughout handles that as "not set"."""
    location_field = values.get(f"day_{block_id_suffix}", {}).get(LOCATION_ACTION_ID, {})
    selected = location_field.get("selected_option")
    location = selected["value"] if selected else None

    client_choice = None
    text = None
    if location == "Client Office":
        select_field = values.get(f"client_select_{block_id_suffix}", {}).get(CLIENT_SELECT_ACTION_ID, {})
        choice_selected = select_field.get("selected_option")
        client_choice = choice_selected["value"] if choice_selected else None
        if client_choice == CUSTOM_CLIENT_VALUE:
            text = values.get(f"client_custom_{block_id_suffix}", {}).get("text", {}).get("value")
        else:
            text = client_choice  # a real client name picked directly from the dropdown
    elif location == "Other":
        text = values.get(f"client_{block_id_suffix}", {}).get("text", {}).get("value")

    return {"location": location, "client_choice": client_choice, "text": text}


def extract_day_state(values: dict) -> dict:
    """Reads the modal's current full state (all 5 days) out of a view's
    state.values -- used both to rebuild blocks on a live field change and to
    parse the final submission, so both paths agree on what's "currently set"."""
    day_state = {}
    for offset in range(5):
        split_field = values.get(f"day_{offset}_split", {}).get(SPLIT_DAY_ACTION_ID, {})
        split = bool(split_field.get("selected_options"))

        day_state[offset] = {
            "split": split,
            "full": _extract_location_field(values, str(offset)),
            "morning": _extract_location_field(values, f"{offset}_morning"),
            "afternoon": _extract_location_field(values, f"{offset}_afternoon"),
        }
    return day_state


def build_week_modal(
    week_start: str, user_name: str, prefill: dict | None = None, title: str | None = None, note: str | None = None
) -> dict:
    """prefill: {offset: {"split": bool, "full": {...}, "morning": {...}, "afternoon": {...}}}
    for pre-filling from existing entries (see _build_day_blocks for the shape
    of each field). title overrides the modal's title (Slack caps plain_text
    titles at 24 chars).
    note, if given, renders as a leading text block above the day fields -- used
    by "Same as last week" to flag anything that couldn't be carried over."""
    blocks = _build_day_blocks(week_start, prefill or {})
    if note:
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": note}},
            {"type": "divider"},
        ] + blocks

    return {
        "type": "modal",
        "callback_id": CALLBACK_ID_WEEK_MODAL,
        "private_metadata": json.dumps(
            {"week_start": week_start, "user_name": user_name, "title": title, "note": note}
        ),
        "title": {"type": "plain_text", "text": title or "Log your week"},
        "submit": {"type": "plain_text", "text": "Save Week"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def rebuild_modal_view(
    week_start: str, user_name: str, day_state: dict, title: str | None = None, note: str | None = None
) -> dict:
    """Same view shape as build_week_modal, but built from the modal's own live
    state (day_state from extract_day_state) rather than DB prefill -- used when
    responding to a location-select change with views.update. title/note are
    carried over from the original private_metadata so a "Same as last week"
    confirmation modal keeps its title/note across live field-change updates."""
    return build_week_modal(week_start, user_name, prefill=day_state, title=title, note=note)


def _parse_location_field(
    field_state: dict, date_str: str, block_id_suffix: str, time_period: str | None, entries: list, errors: dict
) -> None:
    """Turns one location field's state into an EntryCreate (appended to entries),
    or a field-anchored error (added to errors) if a required client/description
    was left blank. Shared by the full-day and each half of a split day."""
    location = field_state["location"]
    if not location:
        return  # left blank -- no entry for this field

    text_value = (field_state["text"] or "").strip() or None

    # The single per-field text value means either "client" (required for
    # Client Office/Other) or free-form "notes" -- matches the web app's own
    # conditional routing (frontend/src/App.tsx:1012-1015).
    entry_kwargs = {"date": date_str, "location": location}
    if time_period:
        entry_kwargs["time_period"] = time_period
    if location in CLIENT_TEXT_LOCATIONS:
        entry_kwargs["client"] = text_value
    else:
        entry_kwargs["notes"] = text_value

    try:
        entries.append(EntryCreate(**entry_kwargs))
    except ValidationError:
        # Client Office/Other require a client name -- EntryCreate's own
        # validator raises for that, but this offers Slack a friendlier,
        # field-anchored inline error instead of a generic one. Anchor it to
        # whichever block is actually currently rendered for this field, or
        # Slack will silently ignore an error pointed at a nonexistent block_id.
        if location == "Client Office":
            choice = field_state["client_choice"]
            block_id = f"client_custom_{block_id_suffix}" if choice == CUSTOM_CLIENT_VALUE else f"client_select_{block_id_suffix}"
        else:
            block_id = f"client_{block_id_suffix}"
        errors[block_id] = "Client name/description is required for this location"


def parse_week_submission(view: dict) -> tuple[list[EntryCreate], dict]:
    """Returns (entries, errors). If errors is non-empty, caller must return a
    Slack response_action:errors payload and must NOT touch the database."""
    metadata = json.loads(view["private_metadata"])
    week_start = metadata["week_start"]
    day_state = extract_day_state(view["state"]["values"])

    entries: list[EntryCreate] = []
    errors: dict[str, str] = {}

    for offset in range(5):
        state = day_state[offset]
        date_str = _day_date(week_start, offset)

        if state["split"]:
            _parse_location_field(state["morning"], date_str, f"{offset}_morning", "Morning", entries, errors)
            _parse_location_field(state["afternoon"], date_str, f"{offset}_afternoon", "Afternoon", entries, errors)
        else:
            _parse_location_field(state["full"], date_str, str(offset), None, entries, errors)

    return entries, errors


TRACKER_URL = os.getenv("TRACKER_URL", "https://in-office.vercel.app")

_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}

ACTION_VIEW_FULL_SCHEDULE = "view_full_schedule"


def _ordinal_day(day: int) -> str:
    if 11 <= day % 100 <= 13:
        suffix = "th"
    else:
        suffix = _ORDINAL_SUFFIXES.get(day % 10, "th")
    return f"{day}{suffix}"


def _see_full_schedule_button() -> dict:
    return {
        # Slack still sends a block_actions payload to our Request URL
        # even for a "url" button -- action_id/value are set so
        # _handle_block_action can recognize and ignore it cleanly
        # instead of crashing on a missing dict key.
        "type": "button",
        "text": {"type": "plain_text", "text": "📅 See Full Schedule", "emoji": True},
        "url": TRACKER_URL,
        "action_id": ACTION_VIEW_FULL_SCHEDULE,
        "value": TRACKER_URL,
    }


def _enter_my_week_button(week_start: str) -> dict:
    """Opens the week modal directly for whoever clicks it, for the given
    week -- reuses ACTION_FILL_WEEK's existing handler in slack_routes.py
    (block click still carries a fresh trigger_id, just like the quick-fill
    DM's "Fill in week" button), so no new routing logic is needed."""
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": "✏️ Fill My Week", "emoji": True},
        "action_id": ACTION_FILL_WEEK,
        "value": week_start,
    }


def _mention(name: str, directory: dict) -> str:
    """A real Slack mention (<@ID>, renders as a clickable @name pill) if this
    person's normalized name matches the Slack directory, else their plain
    display name as inert text -- still informative, just not clickable."""
    match = directory.get(name.strip().lower())
    return f"<@{match['id']}>" if match else f"@{name}"


def _format_entries(rows: list, directory: dict) -> str:
    """Names for one location group, annotated with a time period (e.g.
    "@Name (Morning)") when a row is a split (half) day entry -- matching the
    tracker website's display convention. Without this, someone at Neal Street
    in the morning and a Client Office in the afternoon would just look like an
    unexplained duplicate between the two sections."""
    unique_rows: dict[tuple[str, str], object] = {}
    for row in rows:
        key = (row.user_name.strip().lower(), getattr(row, "time_period", None) or "")
        unique_rows.setdefault(key, row)

    if not unique_rows:
        return "_No one going_"

    def label(row) -> str:
        mention = _mention(row.user_name, directory)
        time_period = getattr(row, "time_period", None)
        return f"{mention} ({time_period})" if time_period else mention

    ordered = sorted(unique_rows.values(), key=lambda r: (r.user_name.strip().lower(), r.time_period or ""))
    return "  ".join(label(row) for row in ordered)


def _format_location_groups(day_rows: list, directory: dict) -> str:
    """Neal Street and Client Office, on separate lines, mirroring the tracker
    website's Who's Where grouping -- Client Office is further broken out by
    client name on its own rows below. Other locations (WFH, Holiday, etc.)
    aren't shown here since these summaries are specifically about who's in
    an office."""
    neal_street_rows = [row for row in day_rows if row.location == "Neal Street"]
    client_rows = [row for row in day_rows if row.location == "Client Office"]

    sections = []
    if neal_street_rows:
        count = len({row.user_name for row in neal_street_rows})
        sections.append(f"🏢 *Neal Street ({count})*\n{_format_entries(neal_street_rows, directory)}")

    if client_rows:
        count = len({row.user_name for row in client_rows})
        by_client: dict[str, list] = {}
        for row in client_rows:
            by_client.setdefault(row.client or "No Client", []).append(row)
        client_lines = "\n".join(
            f"*{client}*: {_format_entries(rows, directory)}" for client, rows in sorted(by_client.items())
        )
        sections.append(f"💼 *Client Office ({count})*\n{client_lines}")

    if not sections:
        return "_No one in the office_"
    return "\n\n".join(sections)


def build_neal_street_week_message(
    week_entries: list,
    week_start: str,
    directory: dict | None = None,
    header_text: str | None = None,
    show_enter_week_button: bool = False,
) -> dict:
    """Officely-style summary: each day clearly separated, Neal Street and
    Client Office (the "who's in an office" question people actually ask),
    with a link to the full tracker for anyone who wants the other locations
    too. header_text lets callers reuse this for a different week (e.g. the
    Friday next-week digest) -- defaults to the standard "this week" wording
    used by the post-submission summary. show_enter_week_button adds a second
    button for whoever's reading to jump straight into entering week_start's
    locations themselves -- on by default for the next-week digest, off for
    the post-submission summary (redundant right after someone just submitted)."""
    directory = directory or {}
    header_text = header_text or "Here's who's in the office this week"
    by_date: dict[str, list] = {}
    for row in week_entries:
        if row.location in ("Neal Street", "Client Office"):
            by_date.setdefault(row.date, []).append(row)

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{header_text}*"},
        },
    ]
    for offset in range(5):
        date_str = _day_date(week_start, offset)
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        day_header = f"{WEEKDAY_NAMES[offset][:3]} {_ordinal_day(date_obj.day)}"
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{day_header}*\n{_format_location_groups(by_date.get(date_str, []), directory)}",
            },
        })

    action_elements = [_see_full_schedule_button()]
    if show_enter_week_button:
        action_elements.append(_enter_my_week_button(week_start))

    blocks.append({"type": "divider"})
    blocks.append({"type": "actions", "elements": action_elements})

    return {"text": header_text, "blocks": blocks}


def _build_single_day_neal_street_message(
    greeting: str, day_label: str, day_rows: list, week_start: str, directory: dict | None = None
) -> dict:
    """Shared shape for a single-day heads-up (today's 9am digest, tomorrow's
    4pm digest): greeting, divider, a day section with Neal Street/Client
    Office broken out on separate lines (always real @mentions via the Slack
    directory when available), divider, "See Full Schedule" + "Enter My Week"
    buttons. week_start is the Monday of the week that day belongs to, for the
    "Enter My Week" button to open the right week's modal."""
    directory = directory or {}
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{greeting}*"}},
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{day_label}*\n{_format_location_groups(day_rows, directory)}",
            },
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [_see_full_schedule_button(), _enter_my_week_button(week_start)],
        },
    ]

    return {"text": greeting, "blocks": blocks}


def _week_start_of(date_obj) -> str:
    monday = date_obj - timedelta(days=date_obj.weekday())
    return monday.strftime("%Y-%m-%d")


def build_neal_street_today_message(date_str: str, day_rows: list, directory: dict | None = None) -> dict:
    """Same visual style as build_neal_street_week_message (bold header, divider,
    day section, real @mentions, "See Full Schedule" + "Enter My Week" buttons)
    but for the single-day 9am same-day digest."""
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    weekday_name = WEEKDAY_NAMES[date_obj.weekday()][:3]
    day_header = f"{weekday_name} {_ordinal_day(date_obj.day)}"
    greeting = ":coffee: Good morning everyone! Here's who will be in the office today :point_down:"
    return _build_single_day_neal_street_message(greeting, day_header, day_rows, _week_start_of(date_obj.date()), directory)


def build_neal_street_tomorrow_message(date_str: str, day_rows: list, directory: dict | None = None) -> dict:
    """Same visual style as build_neal_street_week_message but for the single-day
    4pm heads-up."""
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    weekday_name = WEEKDAY_NAMES[date_obj.weekday()][:3]
    day_header = f"{weekday_name} {_ordinal_day(date_obj.day)}"
    greeting = ":wave: Good afternoon everyone! Here's who will be in the office tomorrow :point_down:"
    return _build_single_day_neal_street_message(greeting, day_header, day_rows, _week_start_of(date_obj.date()), directory)
