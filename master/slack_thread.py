"""Slack thread helpers — read alert context and run investigations."""

from __future__ import annotations

import logging
import re
from typing import Optional

from slack_sdk import WebClient
from master.master_client import MasterAgentClient
from master.slack_api import (
    conversations_history,
    conversations_replies,
    get_channel_meta,
    slack_api_error_hint,
)
from master.alert_parser import (
    AlertContext,
    build_investigation_query,
    enrich_alert_from_text,
    format_slack_reply,
    infer_alert_from_context,
    is_valid_k8s_alert,
    parse_alert_message,
)

BOT_VERSION = "2026-06-03-deployment-pod-fixes"

logger = logging.getLogger(__name__)

_last_resolve_error: str = ""


def strip_bot_mention(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text or "").strip()


def extract_user_prompt(mention_text: str) -> str:
    """Text after @bot mention (optional extra instructions)."""
    text = strip_bot_mention(mention_text)
    text = re.sub(r"^[,\s:;]+", "", text).strip()
    text = re.sub(
        r"^(investigate|check|debug|analyze|help|please)\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    return text


def _is_short_follow_up(user_prompt: str) -> bool:
    """Confirmations and terse follow-ups should not re-post the alert banner."""
    if not (user_prompt or "").strip():
        return False
    try:
        from agents.kubernetes.intent import (
            is_confirmation_no,
            is_confirmation_yes,
            normalize_confirmation,
        )

        norm = normalize_confirmation(user_prompt)
        if is_confirmation_yes(norm) or is_confirmation_no(norm):
            return True
    except Exception:
        pass
    return len(user_prompt.strip()) < 120


def resolve_thread_ts(client: WebClient, channel: str, event: dict) -> str:
    """
    Root thread timestamp for conversations.replies.

    app_mention sometimes omits thread_ts; any message ts inside a thread still works
    for conversations.replies per Slack API.
    """
    thread_ts = (event.get("thread_ts") or "").strip()
    if thread_ts:
        return thread_ts
    ts = (event.get("ts") or "").strip()
    if not ts:
        return ""
    messages, err = conversations_replies(client, channel, ts, limit=5)
    if err:
        global _last_resolve_error
        meta = get_channel_meta(client, channel)
        _last_resolve_error = slack_api_error_hint(err, is_private=meta.get("is_private", False))
    if messages:
        parent = (messages[0].get("thread_ts") or "").strip()
        if parent:
            return parent
    return ts


def _parse_message_alert(message: dict) -> Optional[AlertContext]:
    from master.slack_message_text import extract_message_text
    from master.alert_parser import infer_alert_from_context, parse_alert_message

    body = extract_message_text(message)
    if not body:
        return None
    return parse_alert_message(body) or infer_alert_from_context(body)


def find_alert_in_channel_history(
    client: WebClient,
    channel: str,
    *,
    thread_root_ts: str = "",
    limit: int = 50,
) -> Optional[AlertContext]:
    """Scan recent channel messages when thread replies are empty or truncated."""
    messages, err = conversations_history(client, channel, limit=limit)
    if err:
        global _last_resolve_error
        meta = get_channel_meta(client, channel)
        _last_resolve_error = slack_api_error_hint(err, is_private=meta.get("is_private", False))
        return None
    if thread_root_ts:
        for message in messages:
            ts = (message.get("ts") or "").strip()
            parent = (message.get("thread_ts") or "").strip()
            if ts == thread_root_ts or parent == thread_root_ts:
                alert = _parse_message_alert(message)
                if alert is not None:
                    logger.info("Alert from history ts=%s alertname=%s", ts, alert.alertname)
                    return alert

    for message in messages:
        alert = _parse_message_alert(message)
        if alert is not None and alert.namespace:
            logger.info(
                "Alert from recent history ts=%s alertname=%s namespace=%s",
                message.get("ts"),
                alert.alertname,
                alert.namespace,
            )
            return alert
    return None


def _skip_message_for_alert_parse(message: dict, *, bot_user_id: str = "") -> bool:
    """Skip NOC Handover bot replies so error text does not break alert parsing."""
    if bot_user_id and message.get("user") == bot_user_id:
        return True
    if message.get("bot_id"):
        text = (message.get("text") or "").lower()
        if "noc handover" in text or "could not find alert" in text:
            return True
    text = (message.get("text") or "").lower()
    if "could not find alert fields" in text or "could not find alert fields for" in text:
        return True
    if "noc handover bot" in text and "investigating" in text:
        return True
    return False


def fetch_thread_messages(
    client: WebClient,
    channel: str,
    thread_ts: str,
    *,
    bot_user_id: str = "",
) -> tuple[list[dict], Optional[str]]:
    """Load thread messages for alert parsing."""
    messages, err = conversations_replies(client, channel, thread_ts, limit=100)
    if err:
        return [], err
    return [
        m for m in messages if not _skip_message_for_alert_parse(m, bot_user_id=bot_user_id)
    ], None


def fetch_thread_text(
    client: WebClient, channel: str, thread_ts: str, *, bot_user_id: str = ""
) -> str:
    """Load all messages in a thread for alert parsing (text, blocks, attachments)."""
    from master.slack_message_text import extract_message_text

    messages, err = fetch_thread_messages(
        client, channel, thread_ts, bot_user_id=bot_user_id
    )
    if err:
        global _last_resolve_error
        meta = get_channel_meta(client, channel)
        _last_resolve_error = slack_api_error_hint(err, is_private=meta.get("is_private", False))
        logger.warning("Thread fetch failed channel=%s err=%s private=%s", channel, err, meta.get("is_private"))
        return ""

    chunks: list[str] = []
    for message in messages:
        text = extract_message_text(message).strip()
        if not text:
            continue
        if text.lower() in ("show less", "show more"):
            continue
        chunks.append(text)
    combined = "\n".join(chunks)
    logger.info(
        "Thread fetch channel=%s ts=%s messages=%s chars=%s has_alertname=%s",
        channel,
        thread_ts,
        len(messages),
        len(combined),
        "alertname:" in combined.lower(),
    )
    return combined


def _coerce_valid_alert(alert: Optional[AlertContext]) -> Optional[AlertContext]:
    return alert if is_valid_k8s_alert(alert) else None


def resolve_alert_from_thread(
    client: WebClient,
    channel: str,
    thread_ts: str,
    mention_text: str = "",
    *,
    event: dict | None = None,
    bot_user_id: str = "",
) -> tuple[Optional[AlertContext], str]:
    """
    Parse alert from thread history + optional @mention line.

    Returns (alert, combined_context_text, user_hint).

    user_hint is set when Slack API scopes/membership block reading the thread.
    """
    global _last_resolve_error
    _last_resolve_error = ""

    root_ts = thread_ts
    if event:
        root_ts = resolve_thread_ts(client, channel, event) or thread_ts

    thread_text = ""
    if root_ts:
        thread_text = fetch_thread_text(client, channel, root_ts, bot_user_id=bot_user_id)

    combined = thread_text
    if mention_text:
        combined = f"{combined}\n{mention_text}" if combined else mention_text

    user_prompt = extract_user_prompt(mention_text) if mention_text else ""

    alert = _coerce_valid_alert(parse_alert_message(combined))
    if alert is None:
        alert = _coerce_valid_alert(infer_alert_from_context(combined, user_prompt))

    if alert is None and root_ts:
        messages, _err = fetch_thread_messages(
            client, channel, root_ts, bot_user_id=bot_user_id
        )
        for message in messages:
            alert = _coerce_valid_alert(_parse_message_alert(message))
            if alert is not None:
                break

    if alert is None:
        alert = find_alert_in_channel_history(
            client, channel, thread_root_ts=root_ts
        )
        alert = _coerce_valid_alert(alert)

    if alert is None and combined:
        alert = _coerce_valid_alert(infer_alert_from_context(combined, user_prompt))

    if alert is not None and combined:
        alert = enrich_alert_from_text(alert, combined)
        alert = _coerce_valid_alert(alert)

    hint = _last_resolve_error
    if alert is None and not hint and root_ts and not thread_text:
        meta = get_channel_meta(client, channel)
        if meta.get("is_private"):
            hint = (
                "Could not read this private-channel thread. Ensure Bot scopes "
                "`groups:history`, `groups:read`, event `message.groups`, reinstall app, "
                "and `/invite @NOC Handover`."
            )

    return alert, combined, hint


def build_query(alert: AlertContext, user_prompt: str = "") -> str:
    """Build the agent query. User @mention text drives follow-ups; default uses alert handler."""
    pod = (alert.pod_hint or alert.alert_sre_attributes or "").strip()
    ns = (alert.namespace or "").strip()
    prompt = (user_prompt or "").strip()

    if prompt:
        ctx_parts: list[str] = []
        if pod:
            ctx_parts.append(f"pod/host `{pod}`")
        if ns:
            ctx_parts.append(f"namespace `{ns}`")
        elif pod:
            ctx_parts.append("namespace unknown — search all namespaces for this pod")
        ctx = ", ".join(ctx_parts) if ctx_parts else "cluster"
        return f"{prompt}\n\nKubernetes context: {ctx}."

    return build_investigation_query(alert)


def run_investigation(
    client: WebClient,
    master: MasterAgentClient,
    *,
    channel: str,
    thread_ts: str,
    alert: AlertContext,
    user_prompt: str = "",
) -> None:
    """Post investigation progress and results in the Slack thread."""
    try:
        pod = alert.pod_hint or (alert.alert_sre_attributes or "").strip()
        if pod and alert.namespace:
            scope = f"pod `{pod}` in namespace `{alert.namespace}`"
        elif pod:
            scope = f"pod/host `{pod}` (namespace from alert)"
        elif alert.namespace:
            scope = f"namespace `{alert.namespace}`"
        else:
            scope = "cluster context from alert"
        if not _is_short_follow_up(user_prompt):
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=(
                    f":mag: *NOC handover bot* investigating *{alert.alertname}* "
                    f"— {scope}..."
                ),
            )
        query = build_query(alert, user_prompt)
        pod = (alert.pod_hint or alert.alert_sre_attributes or "").strip() or None
        logger.info(
            "Investigation namespace=%s pod=%s (alert_sre_attributes=%r) query=%s",
            alert.namespace,
            pod,
            alert.alert_sre_attributes,
            query[:240].replace("\n", " "),
        )
        result = master.investigate(
            query,
            alert=alert,
            session_id=f"slack-{channel}-{thread_ts}",
            extra_metadata={"source": "slack"},
        )
        reply = master.format_reply(alert, result, user_prompt=user_prompt)
    except Exception as exc:
        logger.exception("Investigation failed")
        err = str(exc)
        if "Connection refused" in err or "ConnectError" in err:
            reply = (
                f":x: Could not reach the investigation service (`{err}`).\n\n"
                "Start the local stack in separate terminals:\n"
                "```\n./scripts/run-all-agents-local.sh\n./scripts/run-master-agent-local.sh\n./scripts/restart-slack-bot.sh\n```\n"
                "Or port-forward the in-cluster agent and set `MASTER_AGENT_URL`."
            )
        else:
            reply = (
                f":x: Failed to investigate *{alert.alertname}*"
                + (f" in `{alert.namespace}`" if alert.namespace else "")
                + f": {exc}"
            )

    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=reply)
