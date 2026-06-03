"""Slack Web API helpers — private channel scopes, errors, channel access."""

from __future__ import annotations

import logging
from typing import Any, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

# Private channels need groups:* — channels:history does NOT read private threads.
PRIVATE_CHANNEL_BOT_SCOPES = (
    "app_mentions:read",
    "chat:write",
    "groups:history",
    "groups:read",
)

PRIVATE_CHANNEL_BOT_EVENTS = (
    "app_mention",
    "message.groups",
)


def slack_api_error_hint(error: str, *, is_private: bool) -> str:
    """User-facing hint from Slack API error code."""
    if error == "missing_scope":
        if is_private:
            return (
                "I cannot read this *private channel* — the Slack app is missing "
                "`groups:history` and/or `groups:read`. "
                "Add those Bot Token Scopes, subscribe to `message.groups`, reinstall the app, "
                "and `/invite @NOC Handover` again."
            )
        return (
            "I cannot read this channel — missing `channels:history` (or `groups:history` for private)."
        )
    if error == "not_in_channel":
        return "I am not in this channel — run `/invite @NOC Handover` in the private channel."
    if error == "channel_not_found":
        return "Channel not found — confirm the bot is invited to this private channel."
    return f"Slack API error: `{error}`"


def log_slack_api_error(exc: SlackApiError, context: str) -> str:
    data = exc.response if exc.response is not None else {}
    err = data.get("error", str(exc))
    needed = data.get("needed", "")
    provided = data.get("provided", "")
    logger.error(
        "%s failed: error=%s needed=%s provided=%s",
        context,
        err,
        needed,
        provided,
    )
    return err


def get_channel_meta(client: WebClient, channel: str) -> dict[str, Any]:
    """Return is_private, is_member, name — empty dict on failure."""
    try:
        resp = client.conversations_info(channel=channel)
        ch = resp.get("channel") or {}
        return {
            "is_private": bool(ch.get("is_private") or ch.get("is_group")),
            "is_member": bool(ch.get("is_member")),
            "name": ch.get("name") or "",
            "channel_type": "private" if ch.get("is_private") or ch.get("is_group") else "public",
        }
    except SlackApiError as exc:
        log_slack_api_error(exc, "conversations.info")
        return {}


def conversations_replies(
    client: WebClient,
    channel: str,
    ts: str,
    *,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Returns (messages, error_code)."""
    try:
        resp = client.conversations_replies(
            channel=channel,
            ts=ts,
            limit=limit,
            inclusive=True,
        )
        return resp.get("messages") or [], None
    except SlackApiError as exc:
        return [], log_slack_api_error(exc, "conversations.replies")


def conversations_history(
    client: WebClient,
    channel: str,
    *,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    try:
        resp = client.conversations_history(channel=channel, limit=limit)
        return resp.get("messages") or [], None
    except SlackApiError as exc:
        return [], log_slack_api_error(exc, "conversations.history")
