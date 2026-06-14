"""
Minimal Slack reader for the fw-outage channel (read-only).

Used by the freshservice-agent to confirm ongoing outages from internal chatter,
alongside Freshstatus. Uses Slack Web API conversations.history with the bot token.

Env:
  SLACK_BOT_TOKEN              xoxb-... (needs channels:history or groups:history)
  FW_OUTAGE_SLACK_CHANNEL_ID   e.g. C012T9FG8NA

Gracefully disabled when token or channel id is unset — returns [].
The bot must be a member of the channel for history to be readable.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")


class SlackOutageClient:
    def __init__(
        self,
        bot_token: Optional[str] = None,
        channel_id: Optional[str] = None,
    ):
        self._token = (bot_token or os.environ.get("SLACK_BOT_TOKEN", "")).strip()
        self.channel_id = (channel_id or os.environ.get("FW_OUTAGE_SLACK_CHANNEL_ID", "")).strip()

    @property
    def enabled(self) -> bool:
        return bool(self._token and self.channel_id)

    def get_recent_messages(self, hours: int = 24, limit: int = 30) -> list[dict[str, Any]]:
        """
        Return recent fw-outage messages within the last N hours, newest first.
        Each item: {ts, datetime, text, is_threaded}. Returns [] if disabled/error.
        """
        if not self.enabled:
            return []
        oldest = time.time() - hours * 3600
        try:
            resp = httpx.get(
                "https://slack.com/api/conversations.history",
                headers={"Authorization": f"Bearer {self._token}"},
                params={
                    "channel": self.channel_id,
                    "oldest": f"{oldest:.6f}",
                    "limit": str(min(limit, 100)),
                },
                timeout=_TIMEOUT,
            )
            data = resp.json()
        except Exception as exc:
            logger.warning("Slack fw-outage history fetch failed: %s", exc)
            return []

        if not data.get("ok"):
            logger.warning("Slack API error reading fw-outage: %s", data.get("error"))
            return []

        from datetime import datetime, timezone

        out: list[dict[str, Any]] = []
        for m in data.get("messages") or []:
            if m.get("subtype") in ("channel_join", "channel_leave"):
                continue
            text = _MENTION_RE.sub("", m.get("text") or "").strip()
            if not text:
                continue
            ts = m.get("ts") or ""
            try:
                dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                dt = ts
            out.append({
                "ts": ts,
                "datetime": dt,
                "text": text[:600],
                "reply_count": m.get("reply_count", 0),
            })
        out.sort(key=lambda x: x.get("ts") or "", reverse=True)
        return out[:limit]
