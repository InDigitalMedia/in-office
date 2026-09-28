"""Thin httpx-based Slack Web API client + request signature verification.

Deliberately not using slack_sdk/slack-bolt -- httpx is already a dependency, and
these few endpoints don't need a whole framework taking over the request loop.
"""
import hashlib
import hmac
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

# Slack's replay-attack guard: reject requests with an old timestamp.
_MAX_REQUEST_AGE_SECONDS = 60 * 5


def verify_signature(timestamp: str, raw_body: bytes, signature: str, signing_secret: str | None = None) -> bool:
    """Verify a Slack request per https://api.slack.com/authentication/verifying-requests-from-slack."""
    secret = signing_secret if signing_secret is not None else SLACK_SIGNING_SECRET
    if not secret:
        logger.error("SLACK_SIGNING_SECRET not configured -- refusing to verify")
        return False

    try:
        if abs(time.time() - float(timestamp)) > _MAX_REQUEST_AGE_SECONDS:
            logger.warning("Slack request timestamp too old, possible replay")
            return False
    except ValueError:
        return False

    base_string = f"v0:{timestamp}:{raw_body.decode('utf-8')}"
    computed = "v0=" + hmac.new(
        secret.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    is_valid = hmac.compare_digest(computed, signature or "")
    if not is_valid:
        # Never log the secret or signature themselves. Length + whitespace
        # detection is enough to catch the two most common misconfigurations
        # (wrong value copied, or a stray trailing newline/space from paste)
        # without exposing anything sensitive in logs.
        logger.warning(
            f"Slack signature mismatch -- configured secret length={len(secret)}, "
            f"has_surrounding_whitespace={secret != secret.strip()}, "
            f"signature_header_present={bool(signature)}"
        )
    return is_valid


def _headers() -> dict:
    return {"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json; charset=utf-8"}


def post_message(channel: str, text: str, blocks: list | None = None) -> dict:
    payload = {"channel": channel, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks
    resp = httpx.post(f"{SLACK_API_BASE}/chat.postMessage", headers=_headers(), json=payload, timeout=10)
    data = resp.json()
    if not data.get("ok"):
        logger.error(f"chat.postMessage failed: {data}")
    return data


def open_dm(user_id: str) -> str | None:
    """Open (or reuse) a DM channel with a user, returning its channel ID."""
    resp = httpx.post(
        f"{SLACK_API_BASE}/conversations.open", headers=_headers(), json={"users": user_id}, timeout=10
    )
    data = resp.json()
    if not data.get("ok"):
        logger.error(f"conversations.open failed for {user_id}: {data}")
        return None
    return data["channel"]["id"]


def open_view(trigger_id: str, view: dict) -> dict:
    resp = httpx.post(
        f"{SLACK_API_BASE}/views.open", headers=_headers(), json={"trigger_id": trigger_id, "view": view}, timeout=10
    )
    data = resp.json()
    if not data.get("ok"):
        logger.error(f"views.open failed: {data}")
    return data


def update_view(view_id: str, view_hash: str | None, view: dict) -> dict:
    """Re-render an already-open modal (used for the conditional client field)."""
    payload = {"view_id": view_id, "view": view}
    if view_hash:
        payload["hash"] = view_hash
    resp = httpx.post(f"{SLACK_API_BASE}/views.update", headers=_headers(), json=payload, timeout=10)
    data = resp.json()
    if not data.get("ok"):
        logger.error(f"views.update failed: {data}")
    return data


def users_info(user_id: str) -> dict | None:
    resp = httpx.get(f"{SLACK_API_BASE}/users.info", headers=_headers(), params={"user": user_id}, timeout=10)
    data = resp.json()
    if not data.get("ok"):
        logger.error(f"users.info failed for {user_id}: {data}")
        return None
    return data["user"]


def users_list() -> list[dict]:
    """Fetch the full workspace member list, following cursor-based pagination."""
    members = []
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = httpx.get(f"{SLACK_API_BASE}/users.list", headers=_headers(), params=params, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            logger.error(f"users.list failed: {data}")
            break
        members.extend(data.get("members", []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return members


def respond_via_response_url(
    response_url: str, text: str, blocks: list | None = None, replace_original: bool = True, ephemeral: bool = False
) -> None:
    """Update the original interactive message in place via Slack's response_url.
    ephemeral=True instead posts a new reply only the clicking user can see,
    leaving the original untouched -- needed when the original is a shared
    channel post, where replacing it would change it for everyone."""
    if ephemeral:
        payload = {"text": text, "response_type": "ephemeral", "replace_original": False}
    else:
        payload = {"text": text, "replace_original": replace_original}
    if blocks is not None:
        payload["blocks"] = blocks
    try:
        httpx.post(response_url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Failed to post to response_url: {e}")
