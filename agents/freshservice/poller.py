"""
Freshstatus + Freshworks Slack channel 5-minute poller.

Run as a K8s CronJob (every 5 minutes) or directly:
  python -m agents.freshservice.poller

What it does (mirrors n8n scheduled path):
1. Fetch Freshstatus incidents; fingerprint-dedupe to avoid re-alerting.
2. Optionally fetch recent messages from fw-outage Slack channel; dedupe.
3. LLM-summarise new items into a 2–3 line Slack alert.
4. Post to the alert channel via Slack webhook or the existing bot token.

Deduplication uses a local state file (JSON) so the poller can restart
without re-posting. State is intentionally not stored in MySQL to keep
Phase 3 self-contained and stateless-db-friendly.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE_FILE = Path(os.environ.get("POLLER_STATE_FILE", "/tmp/freshstatus-poller-state.json"))
_SEEN_TTL_SECONDS = 6 * 3600  # forget fingerprint after 6 h

# ── State helpers ────────────────────────────────────────────────────────────

def _load_state() -> dict[str, float]:
    """Return {fingerprint: epoch} map from disk."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception as exc:
        logger.warning("State read error: %s", exc)
    return {}


def _save_state(state: dict[str, float]) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state))
    except Exception as exc:
        logger.warning("State write error: %s", exc)


def _prune_state(state: dict[str, float], now: float) -> dict[str, float]:
    return {k: v for k, v in state.items() if now - v < _SEEN_TTL_SECONDS}


def _incident_fingerprint(r: dict[str, Any]) -> str:
    return f"freshstatus:{r.get('id','?')}:{r.get('updated_at','')}"


# ── Freshstatus poller ────────────────────────────────────────────────────────

def poll_freshstatus_new(state: dict[str, float], now: float) -> list[dict[str, Any]]:
    """Return Freshstatus incidents not yet seen (by id+updated_at fingerprint)."""
    from agents.freshservice.freshstatus_client import FreshstatusClient

    client = FreshstatusClient()
    incidents = client.get_recent_incidents(limit=50)
    new_items: list[dict[str, Any]] = []
    for inc in incidents:
        fp = _incident_fingerprint(inc)
        if fp not in state:
            new_items.append(inc)
            state[fp] = now
    return new_items


# ── LLM alert summariser ─────────────────────────────────────────────────────

def _summarise(items: list[dict[str, Any]], channel_label: str) -> str:
    """Ask the LLM for a 2–3 line Slack alert text, or format plainly."""
    try:
        from common.llm import LLMClient

        llm = LLMClient()
        if not llm.enabled():
            return _plain_summary(items, channel_label)
        system = (
            "You are a Freshworks NOC alert bot. Write a concise 2–3 line Slack "
            "message summarising the new Freshstatus incidents below. "
            "Do not use emoji. No bullet points. Professional tone. "
            "Include product, region (if present), and status only."
        )
        user = f"New Freshstatus incidents ({channel_label}):\n{json.dumps(items[:5], default=str)}"
        reply = llm.complete(system=system, user=user, max_tokens=200)
        return reply.strip()
    except Exception as exc:
        logger.warning("LLM summarise failed: %s", exc)
        return _plain_summary(items, channel_label)


def _plain_summary(items: list[dict[str, Any]], label: str) -> str:
    lines = [f"New {label} update(s):"]
    for it in items[:5]:
        title = it.get("title") or it.get("name") or str(it.get("id", "?"))
        status = it.get("status") or ("active" if not it.get("end_time") else "resolved")
        lines.append(f"• {title} — {status}")
    return "\n".join(lines)


# ── Slack poster ─────────────────────────────────────────────────────────────

def _post_to_slack(text: str, channel_id: str) -> None:
    """Post text to Slack via bot token (requires channels:write or incoming webhook)."""
    import httpx

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    webhook_url = os.environ.get("POLLER_SLACK_WEBHOOK_URL", "")

    if webhook_url:
        resp = httpx.post(webhook_url, json={"text": text}, timeout=10)
        resp.raise_for_status()
        logger.info("Posted to Slack via webhook")
        return

    if bot_token and channel_id:
        resp = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {bot_token}"},
            json={"channel": channel_id, "text": text},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack API error: {data.get('error')}")
        logger.info("Posted to Slack channel %s", channel_id)
        return

    logger.warning("No Slack destination configured (set POLLER_SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN+POLLER_ALERT_CHANNEL_ID)")


# ── Main entry point ──────────────────────────────────────────────────────────

def run_once() -> None:
    """Run one polling cycle."""
    now = time.time()
    state = _prune_state(_load_state(), now)
    alert_channel = os.environ.get("POLLER_ALERT_CHANNEL_ID", "")

    new_freshstatus = poll_freshstatus_new(state, now)
    _save_state(state)

    if new_freshstatus:
        logger.info("New Freshstatus items: %d", len(new_freshstatus))
        text = _summarise(new_freshstatus, "Freshstatus")
        if alert_channel or os.environ.get("POLLER_SLACK_WEBHOOK_URL"):
            try:
                _post_to_slack(text, alert_channel)
            except Exception as exc:
                logger.error("Failed to post Freshstatus alert: %s", exc)
        else:
            logger.info("No Slack target configured; alert text:\n%s", text)
    else:
        logger.info("No new Freshstatus items.")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_once()


if __name__ == "__main__":
    main()
