"""
Slack bot — @mention in alert thread → read thread → kubernetes-agent → reply in thread.

Typical flow:
  1. Alert tool (e.g. NOC-Automator) posts KubePodCrashLooping in channel
  2. On-call tags @NOC-handover-bot in that thread (optional: "check logs")
  3. NOC handover bot reads thread context, investigates, replies in same thread

Environment:
  SLACK_BOT_TOKEN           — xoxb-... (OAuth & Permissions → Install App)
  SLACK_APP_TOKEN           — xapp-... (Socket Mode, recommended)
  SLACK_SIGNING_SECRET      — from Basic Information
  SLACK_ALERT_CHANNEL_IDS   — e.g. C0B7R75EQ8K
  SLACK_TRIGGER_MODE        — mention (default) | auto | both
  KUBERNETES_AGENT_URL      — in-cluster agent URL
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from master.alert_parser import (
    K8S_ALERT_HANDLERS,
    is_k8s_alertname,
    parse_alert_message,
)
from master.slack_api import PRIVATE_CHANNEL_BOT_EVENTS, PRIVATE_CHANNEL_BOT_SCOPES, get_channel_meta
from master.master_client import MasterAgentClient
from master.slack_thread import (
    BOT_VERSION,
    _FS_SIGNAL_RE,
    apply_confirmation_decision,
    extract_user_prompt,
    resolve_alert_from_thread,
    run_investigation,
    strip_bot_mention,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _allowed_channels() -> set[str] | None:
    raw = os.environ.get("SLACK_ALERT_CHANNEL_IDS", "").strip()
    if not raw:
        return None
    return {c.strip() for c in raw.split(",") if c.strip()}


def _trigger_mode() -> str:
    return os.environ.get("SLACK_TRIGGER_MODE", "mention").strip().lower()


def _should_handle_alertname(name: str) -> bool:
    """Accept Kube* and NOC-Automator K8s alerts (CrashLoopBackOff, etc.)."""
    if is_k8s_alertname(name):
        return True
    raw = os.environ.get("K8S_ALERT_NAMES", "").strip()
    if raw:
        return name in {a.strip() for a in raw.split(",") if a.strip()}
    return False


def _run_direct(
    client,
    master,
    *,
    channel: str,
    thread_ts: str,
    query: str,
) -> None:
    """Send a query directly to master agent without a K8s alert context."""
    from master.slack_thread import (
        _to_slack_mrkdwn,
        _split_confirm,
        _split_log_artifacts,
        _upload_log_artifacts,
        fetch_thread_messages,
    )
    from master.slack_message_text import extract_message_text

    # Post an immediate acknowledgment so the user isn't waiting in silence
    try:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":mag: Looking up incident details…",
        )
    except Exception:
        pass  # best-effort; don't fail the whole flow

    # Fetch prior thread turns so follow-up questions are contextual
    # ("the 6 incidents you listed", "details on that one"). Best-effort.
    thread_msgs: list[dict] = []
    try:
        from master.slack_thread import strip_bot_mention

        raw, _err = fetch_thread_messages(client, channel, thread_ts)
        q_norm = query.strip().lower()
        for m in raw or []:
            text = strip_bot_mention(extract_message_text(m)).strip()
            if not text:
                continue
            # Skip the current question (it appears in the thread as the latest turn)
            if q_norm and q_norm in text.lower():
                continue
            if text.startswith(":mag:") or text.startswith(":hourglass") or text.startswith(":x:"):
                continue  # skip our own ack / working / error messages
            thread_msgs.append({"role": "assistant" if m.get("bot_id") else "user", "content": text})
    except Exception:
        thread_msgs = []

    try:
        result = master.investigate(
            query,
            session_id=f"fs-{channel}-{thread_ts}",
            extra_metadata={
                "source": "slack",
                "slack_channel": channel,
                "slack_thread_ts": thread_ts,
                "thread_messages": thread_msgs,
            },
        )
        reply = f"_via *{result.agent.name}*_\n{result.answer or '_No details returned._'}"
        reply, confirm = _split_confirm(reply)
        reply, log_artifacts = _split_log_artifacts(reply)
        reply = _to_slack_mrkdwn(reply)
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=reply)
        _upload_log_artifacts(client, channel=channel, thread_ts=thread_ts, artifacts=log_artifacts)
    except Exception as exc:
        err = str(exc)
        if "Connection refused" in err or "ConnectError" in err:
            reply = f":x: Could not reach the investigation service (`{err}`)."
        else:
            reply = f":x: Investigation failed: {exc}"
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=reply)


def create_app() -> App:
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError(
            "SLACK_BOT_TOKEN is required — Slack app → OAuth & Permissions → Install to Workspace"
        )

    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "").strip()
    app = App(token=bot_token, signing_secret=signing_secret or None)
    master = MasterAgentClient()
    try:
        agents = master.list_agents()
        logger.info("Master agent specialists: %s", ", ".join(agents) or "(none)")
    except Exception as exc:
        logger.warning("Master agent registry check failed: %s", exc)
    logger.info(
        "Master URL: %s (empty = in-process router)",
        os.environ.get("MASTER_AGENT_URL", master.base_url or "(local)"),
    )
    channels = _allowed_channels()
    mode = _trigger_mode()

    auth = app.client.auth_test()
    bot_user_id = auth.get("user_id", "")
    logger.info("Slack bot connected as %s (id=%s, trigger=%s)", auth.get("user"), bot_user_id, mode)
    logger.info(
        "Channel filter: %s",
        ", ".join(sorted(channels)) if channels else "disabled (all channels)",
    )
    logger.info("AGENT_URLS (master uses): %s", os.environ.get("AGENT_URLS", "(default)"))
    logger.info("NOC Handover bot version: %s", BOT_VERSION)
    logger.info(
        "Private channel requires bot scopes: %s",
        ", ".join(PRIVATE_CHANNEL_BOT_SCOPES),
    )
    logger.info(
        "Private channel requires bot events: %s",
        ", ".join(PRIVATE_CHANNEL_BOT_EVENTS),
    )
    for cid in channels or []:
        meta = get_channel_meta(app.client, cid)
        if meta:
            logger.info(
                "Configured channel %s: name=%s type=%s is_member=%s",
                cid,
                meta.get("name"),
                meta.get("channel_type"),
                meta.get("is_member"),
            )

    def _channel_ok(channel: str) -> bool:
        return not channels or channel in channels

    def _on_bot_mentioned(event, client, source: str) -> None:
        channel = event.get("channel", "")
        if not _channel_ok(channel):
            logger.warning(
                "Ignored %s in channel=%s (SLACK_ALERT_CHANNEL_IDS=%s)",
                source,
                channel,
                os.environ.get("SLACK_ALERT_CHANNEL_IDS", ""),
            )
            return

        thread_ts = event.get("thread_ts") or event.get("ts")
        mention_text = event.get("text") or ""
        channel_type = event.get("channel_type") or ""
        logger.info(
            "%s channel=%s channel_type=%s event_thread_ts=%s event_ts=%s text=%s",
            source,
            channel,
            channel_type,
            event.get("thread_ts"),
            event.get("ts"),
            strip_bot_mention(mention_text)[:80],
        )
        if channel_type == "group" or (channel and channel.startswith("G")):
            meta = get_channel_meta(client, channel)
            logger.info("Private channel meta: %s", meta)
        _investigate_from_context(client, channel, thread_ts, mention_text, event=event)

    def _investigate_from_context(
        client,
        channel: str,
        thread_ts: str,
        mention_text: str = "",
        *,
        event: dict | None = None,
    ) -> None:
        alert, combined, hint = resolve_alert_from_thread(
            client,
            channel,
            thread_ts,
            mention_text,
            event=event,
            bot_user_id=bot_user_id,
        )
        reply_ts = thread_ts
        if event:
            reply_ts = event.get("thread_ts") or event.get("ts") or thread_ts

        user_prompt = extract_user_prompt(mention_text)

        # If the user is asking about a Freshservice/MIM topic, bypass K8s alert
        # requirement and send directly to the master agent for routing.
        if _FS_SIGNAL_RE.search(user_prompt):
            logger.info("Freshservice signal in mention — bypassing K8s alert check")
            _run_direct(client, master, channel=channel, thread_ts=reply_ts, query=user_prompt)
            return

        if alert is None:
            logger.warning(
                "No alert parsed channel=%s thread_ts=%s context_len=%s preview=%s hint=%s",
                channel,
                thread_ts,
                len(combined),
                combined[:300].replace("\n", " "),
                hint,
            )
            if hint:
                body = hint
            else:
                body = (
                    "I could not find alert fields for this message. "
                    "Reply *in the NOC-Automator alert thread* (not the main channel) with "
                    "`@NOC Handover investigate` or your question. "
                    "I need `alertname:` and `namespace:` from the alert (or a clear crashloop question in that thread)."
                )
            client.chat_postMessage(
                channel=channel,
                thread_ts=reply_ts,
                text=f"{body}\n\n_(bot build: {BOT_VERSION})_",
            )
            return
        if not _should_handle_alertname(alert.alertname):
            client.chat_postMessage(
                channel=channel,
                thread_ts=reply_ts,
                text=f"I do not handle alert `{alert.alertname}` yet.",
            )
            return
        if not alert.is_firing:
            client.chat_postMessage(
                channel=channel,
                thread_ts=reply_ts,
                text=f"Alert `{alert.alertname}` is not firing (status: `{alert.status}`). Skipping.",
            )
            return

        run_investigation(
            client,
            master,
            channel=channel,
            thread_ts=reply_ts,
            alert=alert,
            user_prompt=user_prompt,
        )

    def _apply_decision(body, client, decision: str) -> None:
        container = body.get("container") or {}
        channel = (body.get("channel") or {}).get("id") or container.get("channel_id") or ""
        msg = body.get("message") or {}
        thread_ts = (
            msg.get("thread_ts")
            or container.get("thread_ts")
            or msg.get("ts")
            or container.get("message_ts")
            or ""
        )
        user = (body.get("user") or {}).get("id", "")
        msg_ts = container.get("message_ts") or msg.get("ts")

        label = ":white_check_mark: Approved" if decision == "yes" else ":x: Cancelled"
        who = f" by <@{user}>" if user else ""
        if msg_ts:
            try:
                client.chat_update(
                    channel=channel,
                    ts=msg_ts,
                    text=f"{label}{who}",
                    blocks=[
                        {"type": "section", "text": {"type": "mrkdwn", "text": f"{label}{who}"}}
                    ],
                )
            except Exception as exc:
                logger.warning("Confirm message update failed: %s", exc)

        if not _channel_ok(channel) or not thread_ts:
            return
        apply_confirmation_decision(
            client,
            master,
            channel=channel,
            thread_ts=thread_ts,
            decision=decision,
            bot_user_id=bot_user_id,
        )

    @app.action("noc_confirm_yes")
    def handle_confirm_yes(ack, body, client):
        ack()
        _apply_decision(body, client, "yes")

    @app.action("noc_confirm_no")
    def handle_confirm_no(ack, body, client):
        ack()
        _apply_decision(body, client, "no")

    @app.event("app_mention")
    def handle_mention(event, client):
        """Primary trigger: @bot (requires Event Subscriptions → app_mention)."""
        _on_bot_mentioned(event, client, "app_mention")

    if mode in ("mention", "auto", "both"):

        @app.event("message")
        def handle_message(event, client):
            if event.get("subtype") in (
                "bot_message",
                "message_changed",
                "message_deleted",
            ):
                return
            if event.get("bot_id"):
                return
            if bot_user_id and event.get("user") == bot_user_id:
                return

            text = event.get("text") or ""
            channel = event.get("channel", "")

            # @mention in thread (fallback if app_mention event not subscribed)
            if mode in ("mention", "both") and bot_user_id and f"<@{bot_user_id}>" in text:
                _on_bot_mentioned(event, client, "message_mention")
                return

            if mode not in ("auto", "both"):
                return

            if not _channel_ok(channel):
                return

            alert = parse_alert_message(text)
            if alert is None or not _should_handle_alertname(alert.alertname):
                return
            if not alert.is_firing:
                return

            thread_ts = event.get("thread_ts") or event.get("ts")
            logger.info("Auto-investigate %s in %s", alert.alertname, channel)
            run_investigation(
                client,
                master,
                channel=channel,
                thread_ts=thread_ts,
                alert=alert,
            )

    return app


def main() -> None:
    app = create_app()
    app_token = os.environ.get("SLACK_APP_TOKEN", "").strip()

    if app_token:
        logger.info("Starting Slack bot (Socket Mode)")
        SocketModeHandler(app, app_token).start()
        return

    if not os.environ.get("SLACK_SIGNING_SECRET", "").strip():
        raise RuntimeError(
            "Set SLACK_APP_TOKEN (Socket Mode) or SLACK_SIGNING_SECRET (HTTP mode)"
        )

    port = int(os.environ.get("SLACK_PORT", "3000"))
    logger.info("Starting Slack bot (HTTP) on port %s", port)
    app.start(port=port)


if __name__ == "__main__":
    main()
