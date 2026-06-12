"""
Structured report schema + Slack markdown renderer for the freshservice-agent.

The LLM calls ``submit_report`` as its final tool to deliver a consistent,
exec-ready Slack answer. ``render_report`` converts it to mrkdwn.
"""

from __future__ import annotations

from typing import Any

SEVERITY_EMOJI = {
    "critical": "\U0001f534",  # red circle
    "warning": "\U0001f7e0",   # orange circle
    "info": "\U0001f535",      # blue circle
    "ok": "\U0001f7e2",        # green circle
}

SUBMIT_REPORT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_report",
        "description": (
            "Deliver the FINAL answer to the user as a structured incident briefing. "
            "Call this exactly once, after gathering data with other tools. "
            "Be concise and executive-ready. Never invent URLs, ticket ids, or counts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "headline": {
                    "type": "string",
                    "description": "One line: subject + status, e.g. 'Freshdesk outage – resolved Jun 4, MTTR 47 min'.",
                },
                "severity": {
                    "type": "string",
                    "enum": ["critical", "warning", "info", "ok"],
                },
                "summary": {
                    "type": "string",
                    "description": "2–4 sentences: what happened, impacted product/region, customer impact, current status.",
                },
                "key_facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "3–7 bullet points: ticket id, PIR number, product, region, priority, MTTA/MTTD/MTTR, assignee, start/end time. No invented values.",
                },
                "timeline": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Chronological timeline entries — each string is '[HH:MM AM/PM] <what happened>'. "
                        "Populate this from get_pir's timeline_events field when the user asks for timeline, "
                        "incident chronology, 'what happened step by step', or 'who did what'. "
                        "List all significant events. Leave empty for summary/briefing questions."
                    ),
                },
                "personnel": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "People involved in the incident — responders, engineers, managers. "
                        "Populate from get_pir's personnel field when the user asks 'who was involved', "
                        "'who responded', 'who was on the call', 'who handled it'. "
                        "Leave empty for other question types."
                    ),
                },
                "root_cause": {
                    "type": "string",
                    "description": "Root cause or cause segment from data, or 'not available in retrieved data'.",
                },
                "next_steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recommended follow-up actions, or empty list if resolved/informational.",
                },
                "links": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Verbatim URLs from data (status page, FR ticket, Zoom, Slack thread). Copy exactly.",
                },
            },
            "required": ["headline", "severity", "summary"],
        },
    },
}


def render_report(data: dict[str, Any]) -> str:
    """Convert a submit_report tool call payload into Slack mrkdwn."""
    severity = (data.get("severity") or "info").lower()
    emoji = SEVERITY_EMOJI.get(severity, SEVERITY_EMOJI["info"])
    headline = (data.get("headline") or "").strip()
    summary = (data.get("summary") or "").strip()
    key_facts: list[str] = data.get("key_facts") or []
    timeline: list[str] = data.get("timeline") or []
    personnel: list[str] = data.get("personnel") or []
    root_cause = (data.get("root_cause") or "").strip()
    next_steps: list[str] = data.get("next_steps") or []
    links: list[str] = data.get("links") or []

    lines: list[str] = []
    lines.append(f"{emoji} *{headline}*")
    lines.append("")
    if summary:
        lines.append(summary)

    if key_facts:
        lines.append("")
        lines.append("*Key details*")
        for fact in key_facts:
            lines.append(f"• {fact}")

    if timeline:
        lines.append("")
        lines.append("*Incident timeline*")
        for entry in timeline:
            lines.append(f"• {entry}")

    if personnel:
        lines.append("")
        lines.append("*Personnel involved*")
        lines.append(", ".join(personnel))

    if root_cause and root_cause.lower() not in ("", "not available in retrieved data"):
        lines.append("")
        lines.append(f"*Root cause:* {root_cause}")

    if next_steps:
        lines.append("")
        lines.append("*Next steps*")
        for step in next_steps:
            lines.append(f"• {step}")

    if links:
        lines.append("")
        lines.append("*Links*")
        for link in links:
            lines.append(f"• {link}")

    return "\n".join(lines)
