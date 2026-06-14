"""
Bypass the LLM for common incident questions — sub-second answers when data is cached.

Enable/disable: FRESHSERVICE_FAST_PATH=true (default on).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from dataclasses import dataclass

from agents.freshservice.mcp_server import FreshserviceToolDispatcher, ToolResult
from agents.freshservice.report import SEVERITY_EMOJI

logger = logging.getLogger(__name__)

_MI_ID_RE = re.compile(r"\bMI[-_]?(\d{5,8})\b", re.IGNORECASE)
_WHO_RE = re.compile(
    r"\b(who|involved|responder|on[-\s]?call|owner|assigned|joined|personnel|people|team|engineer|lead)\b",
    re.I,
)
_TIMELINE_RE = re.compile(
    r"\b(timeline|chronolog|what happened|sequence|when did|steps|walk.?me.?through|history)\b",
    re.I,
)
_METRICS_RE = re.compile(r"\b(mttr|mttd|mtta|duration|how long|time.?to)\b", re.I)
_DETAIL_RE = re.compile(
    r"\b(root.?cause|rca|impact|details|briefing|outage on|incident on|post.?incident|pir)\b",
    re.I,
)


@dataclass
class AgentResult:
    answer: str
    route: str = "fast"
    steps: int = 0


def _classify_intent(query: str) -> set[str]:
    intents: set[str] = set()
    if _WHO_RE.search(query):
        intents.add("who")
    if _TIMELINE_RE.search(query):
        intents.add("timeline")
    if _METRICS_RE.search(query):
        intents.add("metrics")
    if _DETAIL_RE.search(query):
        intents.add("detail")
    if _MI_ID_RE.search(query):
        intents.add("detail")
    return intents


def _extract_mi_id(query: str) -> Optional[str]:
    m = _MI_ID_RE.search(query)
    return m.group(1) if m else None


def _render_ongoing_outage(result: ToolResult) -> Optional[str]:
    data = result.data if isinstance(result.data, dict) else {}
    if not data:
        return None
    active = data.get("freshstatus_active") or []
    active_count = int(data.get("freshstatus_active_count") or 0)
    slack_msgs = data.get("fw_outage_recent_messages") or []
    if active_count == 0 and not slack_msgs:
        return (
            "No ongoing outages right now. Freshstatus shows all systems operational "
            "and there are no recent messages in the fw-outage channel."
        )
    lines: list[str] = []
    if active_count > 0:
        lines.append(f":red_circle: *{active_count} active Freshstatus incident(s) right now:*")
        for inc in active[:5]:
            title = inc.get("name") or inc.get("title") or "Untitled"
            status = inc.get("status") or ""
            updated = str(inc.get("updated_at") or inc.get("created_at") or "")[:16]
            lines.append(f"• *{title}*  |  status: {status}  |  updated: {updated}")
    else:
        lines.append(":large_green_circle: Freshstatus shows no active incidents.")
    if slack_msgs:
        lines.append(f"\n:slack: *fw-outage Slack ({len(slack_msgs)} recent message(s)):*")
        for msg in slack_msgs[:3]:
            text = (msg.get("text") or "").strip()[:200]
            if text:
                lines.append(f"> {text}")
    elif data.get("fw_outage_slack_available") is False:
        lines.append(
            "_(fw-outage Slack channel not accessible — Freshstatus is the authoritative source)_"
        )
    return "\n".join(lines)

_ONGOING_RE = re.compile(
    r"\b("
    r"ongoing\s+outage|active\s+incident|current\s+outage|any\s+outage|"
    r"are\s+we\s+down|systems?\s+down|outage\s+right\s+now|"
    r"anything\s+down|status\s+page|freshstatus"
    r")\b",
    re.I,
)

_FRESHSTATUS_ONLY_RE = re.compile(
    r"\b(active\s+incidents?|freshstatus|public\s+incidents?|status\s+page)\b",
    re.I,
)


def fast_path_enabled() -> bool:
    flag = os.environ.get("FRESHSERVICE_FAST_PATH", "true").strip().lower()
    return flag in ("1", "true", "yes", "on")


def _mi_id_from_thread(thread_messages: list[dict] | None) -> Optional[str]:
    if not thread_messages:
        return None
    for msg in reversed(thread_messages):
        text = (msg.get("content") or msg.get("text") or "")
        m = _MI_ID_RE.search(text)
        if m:
            return m.group(1)
    return None


def render_pir_brief(pir: dict[str, Any], *, intents: set[str]) -> str:
    """Deterministic Slack briefing from get_pir payload — no LLM."""
    if pir.get("error"):
        return f":x: Could not load PIR — {pir['error']}"

    subject = (pir.get("subject") or "Major Incident").strip()
    tid = pir.get("ticket_id") or "?"
    severity = "warning" if not pir.get("incident_end_time") else "ok"
    emoji = SEVERITY_EMOJI.get(severity, "")

    lines: list[str] = [f"{emoji} *{subject}*  (`MI-{tid}`)"]

    facts: list[str] = []
    if pir.get("product"):
        facts.append(f"Product: {pir['product']}")
    if pir.get("issue_category"):
        facts.append(f"Category: {pir['issue_category']}")
    if pir.get("incident_start_time"):
        facts.append(f"Start: {pir['incident_start_time']}")
    if pir.get("incident_end_time"):
        facts.append(f"End: {pir['incident_end_time']}")
    if pir.get("impact_to_customer"):
        facts.append(f"Customer impact: {str(pir['impact_to_customer'])[:200]}")

    wants_metrics = "metrics" in intents or "detail" in intents
    if wants_metrics:
        for label, key in (
            ("MTTA", "mtta_minutes"),
            ("MTTD", "mttd_minutes"),
            ("MTTR", "mttr_minutes"),
        ):
            val = pir.get(key)
            if val is not None:
                facts.append(f"{label}: {val} min")

    if facts:
        lines.append("")
        lines.extend(f"• {f}" for f in facts)

    if "timeline" in intents or ("detail" in intents and pir.get("timeline_events")):
        events = pir.get("timeline_events") or []
        if events:
            lines.append("")
            lines.append("*Timeline*")
            for ev in events[:12]:
                t = ev.get("time") or "?"
                e = (ev.get("event") or "")[:180]
                lines.append(f"• `{t}` — {e}")

    if "who" in intents or ("detail" in intents and pir.get("personnel_hint")):
        people = pir.get("personnel_hint") or pir.get("personnel") or []
        if people:
            lines.append("")
            lines.append("*People involved*")
            for name in people[:15]:
                lines.append(f"• {name}")

    if "detail" in intents and pir.get("pir_narrative"):
        narrative = str(pir["pir_narrative"]).strip()
        if narrative and "timeline" not in intents:
            snippet = narrative[:600] + ("…" if len(narrative) > 600 else "")
            lines.append("")
            lines.append("*Summary*")
            lines.append(snippet)

    if pir.get("pir_url"):
        lines.append("")
        lines.append(f"<{pir['pir_url']}|View PIR in Freshservice>")

    src = pir.get("source")
    if src:
        lines.append(f"_({src})_")

    return "\n".join(lines).strip()


def try_fast_path(
    query: str,
    *,
    thread_messages: list[dict] | None,
    dispatcher: FreshserviceToolDispatcher,
) -> Optional[AgentResult]:
    """Return an answer without calling the LLM, or None to fall through."""
    if not fast_path_enabled():
        return None

    q = (query or "").strip()
    if not q:
        return None

    intents = _classify_intent(q)
    mi_id = _extract_mi_id(q) or _mi_id_from_thread(thread_messages)

    # 1) Ongoing outage — single tool, template render (~1–3s)
    if _ONGOING_RE.search(q) and not mi_id:
        logger.info("Fast-path: check_ongoing_outages")
        result = dispatcher.call_tool("check_ongoing_outages", {"hours": 24})
        rendered = _render_ongoing_outage(result)
        if rendered:
            return AgentResult(answer=rendered, route="fast-ongoing", steps=0)

    # 2) Freshstatus-only (no ticket id, no complex search)
    if _FRESHSTATUS_ONLY_RE.search(q) and not mi_id and not intents - {"detail"}:
        logger.info("Fast-path: get_freshstatus_incidents")
        result = dispatcher.call_tool("get_freshstatus_incidents", {"active_only": True})
        data = result.data if isinstance(result.data, dict) else {}
        active = data.get("active") or []
        count = int(data.get("active_count") or len(active))
        if count == 0:
            return AgentResult(
                answer=":large_green_circle: No active Freshstatus incidents — all systems operational.",
                route="fast-freshstatus",
                steps=0,
            )
        lines = [f":red_circle: *{count} active Freshstatus incident(s):*"]
        for inc in active[:8]:
            title = inc.get("name") or inc.get("title") or "Untitled"
            status = inc.get("status") or ""
            lines.append(f"• *{title}*  ({status})")
        return AgentResult(answer="\n".join(lines), route="fast-freshstatus", steps=0)

    # 3) Specific MIM ticket — get_pir + template (~0.05s cached, ~2s live)
    if mi_id:
        logger.info("Fast-path: get_pir ticket=%s intents=%s", mi_id, sorted(intents))
        result = dispatcher.call_tool("get_pir", {"ticket_id": mi_id})
        pir = result.data if isinstance(result.data, dict) else {}
        if pir and not pir.get("error"):
            return AgentResult(
                answer=render_pir_brief(pir, intents=intents or {"detail"}),
                route="fast-pir",
                steps=0,
            )

    return None
