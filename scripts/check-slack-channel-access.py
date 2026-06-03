#!/usr/bin/env python3
"""Verify Slack bot can read a private/public alert channel (scopes + membership)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from master.slack_api import (
    PRIVATE_CHANNEL_BOT_EVENTS,
    PRIVATE_CHANNEL_BOT_SCOPES,
    conversations_history,
    conversations_replies,
    get_channel_meta,
)


def main() -> int:
    env_file = ROOT / ".env.local"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = (
        os.environ.get("SLACK_TEST_CHANNEL_ID", "").strip()
        or os.environ.get("SLACK_ALERT_CHANNEL_IDS", "").split(",")[0].strip()
    )
    if not token:
        print("Set SLACK_BOT_TOKEN in .env.local", file=sys.stderr)
        return 1
    if not channel:
        print("Set SLACK_ALERT_CHANNEL_IDS or SLACK_TEST_CHANNEL_ID", file=sys.stderr)
        return 1

    client = WebClient(token=token)
    auth = client.auth_test()
    print(f"Bot: {auth.get('user')} team={auth.get('team')}")

    meta = get_channel_meta(client, channel)
    print(f"Channel {channel}: {meta}")

    if meta.get("is_private"):
        print("\nRequired for PRIVATE channels:")
        print("  Bot scopes:", ", ".join(PRIVATE_CHANNEL_BOT_SCOPES))
        print("  Bot events:", ", ".join(PRIVATE_CHANNEL_BOT_EVENTS))
    else:
        print("\nPublic channel needs: channels:history, channels:read, app_mentions:read, chat:write")

    if not meta.get("is_member"):
        print("\nFAIL: Bot is not a member — run /invite @NOC Handover in that channel")
        return 2

    messages, err = conversations_history(client, channel, limit=5)
    if err:
        print(f"\nFAIL conversations.history: {err}")
        if meta.get("is_private"):
            print("  → Add groups:history + groups:read, reinstall app")
        return 3
    print(f"\nOK conversations.history: {len(messages)} recent message(s)")
    for msg in messages[:3]:
        ts = msg.get("ts", "")
        preview = (msg.get("text") or "")[:60].replace("\n", " ")
        print(f"  ts={ts} preview={preview!r}")

    if messages:
        ts = messages[0]["ts"]
        replies, err2 = conversations_replies(client, channel, ts, limit=10)
        if err2:
            print(f"\nFAIL conversations.replies: {err2}")
            return 4
        print(f"OK conversations.replies on latest ts: {len(replies)} message(s)")

    print("\nIf history works but @mention still fails, reply in the alert *thread* (not channel root).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SlackApiError as exc:
        data = exc.response or {}
        print(f"Slack API error: {data.get('error')} needed={data.get('needed')}", file=sys.stderr)
        raise SystemExit(5) from exc
