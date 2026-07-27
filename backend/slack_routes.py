"""The 3 Slack-facing FastAPI routes: slash command, interactivity, scheduler trigger.

Included into app.py via app.include_router(slack_router).
"""
import json
import logging
import os
from datetime import datetime
from urllib.parse import parse_qsl

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlmodel import Session

import clients
import daily_notifications
import entries as entries_module
import queries
import roster
import slack_client
import slack_directory
import slack_views
from db import get_session

logger = logging.getLogger(__name__)

slack_router = APIRouter()

SLACK_SCHEDULER_SECRET = os.getenv("SLACK_SCHEDULER_SECRET")


def require_scheduler(x_scheduler_secret: str | None = Header(default=None)):
    """Gate the scheduler-triggered endpoint behind its own secret, separate from
    ADMIN_SECRET, to scope blast radius (mirrors require_admin in app.py)."""
    if not SLACK_SCHEDULER_SECRET:
        raise HTTPException(status_code=503, detail="Scheduler endpoint not configured")
    if x_scheduler_secret != SLACK_SCHEDULER_SECRET:
        raise HTTPException(status_code=403, detail="Invalid scheduler secret")


async def _verify_slack_request(request: Request) -> bytes:
    """Verify the request signature and return the raw body. Raises 401 on failure."""
    raw = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not slack_client.verify_signature(timestamp, raw, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")
    return raw


def _resolve_identity(slack_user_id: str) -> tuple[str, bool]:
    """Returns (user_name, matched_roster). Falls back to Slack's own real_name,
    unmatched, if it doesn't correspond to a roster entry."""
    slack_user = slack_client.users_info(slack_user_id)
    profile = (slack_user or {}).get("profile", {})
    real_name = profile.get("real_name_normalized") or profile.get("real_name") or (slack_user or {}).get("name", "")

    normalized_real = real_name.strip().lower()
    for candidate in roster.get_roster():
        if candidate.strip().lower() == normalized_real:
            return candidate, True
    return real_name, False


def _prefill_entry_from_row(row) -> dict:
    """{"location": str, "client_choice": str|None, "text": str} for one saved
    entry (an Entry row, or the "full" slot from get_last_week_entries_for_user
    -- both expose .location/.client/.notes). client_choice must be set too
    (not just text) -- _build_day_blocks uses client_choice, not text, to decide
    the dropdown's initial_option and whether to show the custom-name field. A
    previously saved client that isn't in the predefined list is treated as the
    custom "Other (type below)" choice, so its name still round-trips correctly."""
    entry = {"location": row.location, "client_choice": None, "text": ""}
    if row.location == "Client Office":
        client_value = row.client or ""
        entry["client_choice"] = client_value if client_value in clients.get_clients() else slack_views.CUSTOM_CLIENT_VALUE
        entry["text"] = client_value
    elif row.location == "Other":
        entry["text"] = row.client or ""
    else:
        entry["text"] = row.notes or ""
    return entry


def _build_prefill(session: Session, user_key: str, week_start: str) -> dict:
    """{offset: {"split": bool, "full": {...}, "morning": {...}, "afternoon": {...}}}
    for this user's existing entries this week, for pre-filling the modal --
    split days are fully represented (checkbox pre-checked, both halves
    pre-filled), same as full days."""
    start_date = datetime.strptime(week_start, "%Y-%m-%d").date()
    prefill: dict = {}
    for row in queries.get_week_entries(session, week_start):
        if row.user_name.strip().lower() != user_key:
            continue
        row_date = datetime.strptime(row.date, "%Y-%m-%d").date()
        offset = (row_date - start_date).days
        if not (0 <= offset <= 4):
            continue

        day = prefill.setdefault(offset, {"split": False, "full": {}, "morning": {}, "afternoon": {}})
        if row.time_period == "Morning":
            day["split"] = True
            day["morning"] = _prefill_entry_from_row(row)
        elif row.time_period == "Afternoon":
            day["split"] = True
            day["afternoon"] = _prefill_entry_from_row(row)
        else:
            day["full"] = _prefill_entry_from_row(row)
    return prefill


def _build_prefill_from_last_week(session: Session, user_key: str, week_start: str) -> dict:
    """Same prefill shape as _build_prefill, but sourced from last week's
    entries instead of this week's -- used to pre-populate the "Same as last
    week" confirmation modal so the user can review before saving, rather
    than saving blind. Split days are carried over just like full days."""
    last_week = queries.get_last_week_entries_for_user(session, user_key, week_start)
    prefill: dict = {}
    for offset, slot in last_week.items():
        if slot["morning"] or slot["afternoon"]:
            day = {"split": True, "full": {}, "morning": {}, "afternoon": {}}
            if slot["morning"]:
                day["morning"] = _prefill_entry_from_row(slot["morning"])
            if slot["afternoon"]:
                day["afternoon"] = _prefill_entry_from_row(slot["afternoon"])
        elif slot["full"]:
            day = {"split": False, "full": _prefill_entry_from_row(slot["full"]), "morning": {}, "afternoon": {}}
        else:
            continue
        prefill[offset] = day
    return prefill


def _current_week_start() -> str:
    return daily_notifications.monday_of(datetime.now(daily_notifications.LONDON_TZ).date())


def _open_week_modal(session: Session, trigger_id: str, user_id: str, week_start: str) -> None:
    """Resolve identity, build prefill, open the day-by-day modal. Must run
    synchronously (not via BackgroundTasks) -- trigger_id is only valid for ~3
    seconds from issuance, so this needs to happen while it's still fresh."""
    resolved_name, _matched = _resolve_identity(user_id)
    user_key = resolved_name.strip().lower()
    prefill = _build_prefill(session, user_key, week_start)
    slack_client.open_view(trigger_id, slack_views.build_week_modal(week_start, resolved_name, prefill))


@slack_router.post("/slack/commands")
async def slack_slash_command(request: Request, session: Session = Depends(get_session)):
    raw = await _verify_slack_request(request)
    fields = dict(parse_qsl(raw.decode("utf-8")))
    user_id = fields.get("user_id", "")
    trigger_id = fields.get("trigger_id", "")

    # Straight to the modal -- a slash command has a fresh trigger_id, so there's
    # no need for the intermediate quick-fill button step here (that's only
    # needed by the daily reminder DM, which can't obtain a trigger_id on its own).
    _open_week_modal(session, trigger_id, user_id, _current_week_start())
    # A genuinely empty body, not JSONResponse({}) -- Slack's slash-command
    # contract treats a JSON object lacking "text"/"blocks" as a malformed
    # message to render (which showed up as a literal "{}" bubble), whereas an
    # empty 200 is the documented way to acknowledge with nothing posted.
    return Response(status_code=200)


@slack_router.post("/slack/interactivity")
async def slack_interactivity(
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    raw = await _verify_slack_request(request)
    fields = dict(parse_qsl(raw.decode("utf-8")))
    payload = json.loads(fields["payload"])
    payload_type = payload.get("type")

    if payload_type == "block_actions":
        return _handle_block_action(session, payload)
    if payload_type == "view_submission":
        return _handle_view_submission(session, payload, background_tasks)

    # Anything else (e.g. a shortcut we haven't wired up) -- ack defensively.
    return Response(status_code=200)


def _handle_block_action(session: Session, payload: dict) -> Response:
    action = payload["actions"][0]
    action_id = action.get("action_id")

    # A day's location select (or, for Client Office, its client dropdown)
    # changing inside an already-open modal -- rebuild its blocks (to show/hide
    # the relevant client field) rather than treating this like a quick-fill button.
    if action_id in slack_views.DISPATCH_ACTION_IDS:
        return _handle_location_change(payload)

    # "See Full Schedule" is a url-type button -- Slack still sends us a
    # block_actions payload for it (it just also opens the link client-side),
    # but there's nothing for us to do with it. Any other action_id we don't
    # recognize is handled the same defensive way: ack and move on, rather
    # than assuming a "value"/"user" shape that a button like this doesn't have.
    if action_id not in (slack_views.ACTION_FILL_WEEK, slack_views.ACTION_SAME_AS_LAST_WEEK):
        if action_id != slack_views.ACTION_VIEW_FULL_SCHEDULE:
            logger.warning(f"Unhandled block_actions action_id: {action_id}")
        return Response(status_code=200)

    week_start = action.get("value")
    user_id = payload["user"]["id"]
    response_url = payload.get("response_url")
    trigger_id = payload.get("trigger_id")

    resolved_name, _matched = _resolve_identity(user_id)
    user_key = resolved_name.strip().lower()

    if action_id == slack_views.ACTION_FILL_WEEK:
        prefill = _build_prefill(session, user_key, week_start)
        slack_client.open_view(trigger_id, slack_views.build_week_modal(week_start, resolved_name, prefill))
        return Response(status_code=200)

    if action_id == slack_views.ACTION_SAME_AS_LAST_WEEK:
        # Opens the same modal used everywhere else, pre-filled from last
        # week's entries (split days included), so the user reviews and
        # explicitly submits rather than having a week saved without ever
        # seeing it -- the normal view_submission path (upsert + confirmation
        # DM + week summary) handles the rest exactly as it does for "Fill in
        # week".
        prefill = _build_prefill_from_last_week(session, user_key, week_start)
        if not prefill:
            if response_url:
                slack_client.respond_via_response_url(response_url, "No matching entries found last week -- try Fill in week instead.")
            return Response(status_code=200)

        view = slack_views.build_week_modal(week_start, resolved_name, prefill, title="Same as last week")
        slack_client.open_view(trigger_id, view)
        return Response(status_code=200)

    return Response(status_code=200)


def _handle_location_change(payload: dict) -> Response:
    """A day's location select changed while the modal is still open -- rebuild
    the view so that day's client/description field appears or disappears, then
    push it back via views.update. No DB access needed here."""
    view = payload["view"]
    metadata = json.loads(view["private_metadata"])
    day_state = slack_views.extract_day_state(view["state"]["values"])
    updated_view = slack_views.rebuild_modal_view(
        metadata["week_start"], metadata["user_name"], day_state, title=metadata.get("title"), note=metadata.get("note")
    )
    slack_client.update_view(view["id"], view.get("hash"), updated_view)
    return Response(status_code=200)


def _handle_view_submission(session: Session, payload: dict, background_tasks: BackgroundTasks) -> Response:
    view = payload["view"]
    if view.get("callback_id") != slack_views.CALLBACK_ID_WEEK_MODAL:
        return Response(status_code=200)

    entries_list, errors = slack_views.parse_week_submission(view)
    if errors:
        return JSONResponse({"response_action": "errors", "errors": errors})

    metadata = json.loads(view["private_metadata"])
    user_name = metadata["user_name"]
    week_start = metadata["week_start"]

    try:
        entries_module.upsert_entries(session, user_name, entries_list)
    except ValueError as e:
        return JSONResponse({"response_action": "errors", "errors": {"day_0": str(e)}})

    user_id = payload["user"]["id"]
    background_tasks.add_task(_send_confirmation_dm, session, user_id, week_start)
    return Response(status_code=200)


def _build_week_summary_message(session: Session, week_start: str) -> dict:
    directory = slack_directory.build_directory()
    week_entries = queries.get_week_entries(session, week_start)
    return slack_views.build_neal_street_week_message(week_entries, week_start, directory)


def _send_confirmation_dm(session: Session, user_id: str, week_start: str) -> None:
    dm_channel = slack_client.open_dm(user_id)
    if not dm_channel:
        logger.error(f"Could not open DM with {user_id} to confirm their saved week")
        return
    slack_client.post_message(dm_channel, "✅ Week successfully entered!")
    try:
        message = _build_week_summary_message(session, week_start)
        slack_client.post_message(dm_channel, message["text"], blocks=message["blocks"])
    except Exception as e:
        # The save itself already succeeded (upsert_entries ran before this
        # background task started) -- only the follow-up summary failed, so
        # let them know that specifically rather than silently dropping it.
        logger.error(f"Failed to send week summary DM: {e}")
        slack_client.post_message(dm_channel, "⚠️ (Couldn't load the who's-where-this-week summary right now.)")


@slack_router.post("/internal/slack/daily-digest", dependencies=[Depends(require_scheduler)])
def trigger_today_digest(force: bool = False, session: Session = Depends(get_session)):
    return daily_notifications.run_today_digest(session, force=force)


@slack_router.post("/internal/slack/unfilled-reminders", dependencies=[Depends(require_scheduler)])
def trigger_unfilled_reminders(force: bool = False, session: Session = Depends(get_session)):
    return daily_notifications.run_unfilled_reminders(session, force=force)


@slack_router.post("/internal/slack/afternoon-unfilled-reminders", dependencies=[Depends(require_scheduler)])
def trigger_afternoon_unfilled_reminders(force: bool = False, session: Session = Depends(get_session)):
    return daily_notifications.run_afternoon_unfilled_reminders(session, force=force)


@slack_router.post("/internal/slack/tomorrow-digest", dependencies=[Depends(require_scheduler)])
def trigger_tomorrow_digest(force: bool = False, session: Session = Depends(get_session)):
    return daily_notifications.run_tomorrow_digest(session, force=force)


@slack_router.post("/internal/slack/next-week-reminder", dependencies=[Depends(require_scheduler)])
def trigger_next_week_reminder(force: bool = False, session: Session = Depends(get_session)):
    return daily_notifications.run_next_week_reminder(session, force=force)
