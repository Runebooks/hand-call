"""
Structured report schema + Slack markdown renderer for the freshservice-agent.

The LLM calls ``submit_report`` as its final tool to deliver the answer.

Design philosophy: answer PRECISELY what the user asked, like a knowledgeable
colleague — not a rigid form. The primary field is ``answer`` (a direct,
self-contained, conversational reply). The structured sections (key_facts,
timeline, personnel, links) are OPTIONAL and should only be populated when the
user's question specifically calls for that data. ``render_report`` shows the
answer first and appends only the non-empty optional sections.
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
            "Deliver the FINAL answer to the user. Call exactly once, after gathering data. "
            "Answer PRECISELY what was asked — like a knowledgeable colleague replying in chat, "
            "NOT a rigid incident form. Put the complete answer in `answer`. "
            "Only attach the optional structured sections (key_facts, timeline, personnel, links) "
            "when the user's question specifically calls for that kind of data. "
            "Do NOT dump a full briefing for a focused question. Never invent facts, URLs, or IDs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": (
                        "The direct, precise, conversational answer to EXACTLY what the user asked. "
                        "Self-contained: the reader should get their answer here without needing any "
                        "optional section. Keep it tight — typically 1–4 sentences for a focused "
                        "question (e.g. 'what was the root cause?'); a short paragraph for a briefing. "
                        "Lead with the direct answer. Ground every fact in retrieved data "
                        "(PIR narrative, ticket custom fields, conversations). If the data does not "
                        "contain the answer, say so plainly and state where you looked."
                    ),
                },
                "severity": {
                    "type": "string",
                    "enum": ["critical", "warning", "info", "ok"],
                    "description": (
                        "Optional — sets the status emoji. Use 'critical'/'warning' for active issues, "
                        "'ok' for resolved, 'info' for neutral Q&A. Omit if unsure."
                    ),
                },
                "key_facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "OPTIONAL. A few supporting bullets (ticket id, PIR no., product, region, "
                        "priority, MTTR). Include ONLY for briefing-style questions or when the user "
                        "asks for details/metrics. Omit for a focused single-fact question."
                    ),
                },
                "timeline": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "OPTIONAL. Chronological '[HH:MM] event' strings from the PIR. "
                        "Populate ONLY when the user asks for the timeline, chronology, sequence, "
                        "or 'what happened step by step'. Otherwise leave empty."
                    ),
                },
                "personnel": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "OPTIONAL. People involved, each as 'Name — role' read from the PIR narrative. "
                        "Populate ONLY when the user asks who was involved / who responded / who was on "
                        "the call. Otherwise leave empty."
                    ),
                },
                "links": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "OPTIONAL. Verbatim URLs (status page, PIR, Slack thread, Zoom) copied exactly "
                        "from data. Include ONLY when the user asks for links/status page/thread, or when "
                        "a link is essential to the answer. Omit otherwise."
                    ),
                },
            },
            "required": ["answer"],
        },
    },
}


def render_report(data: dict[str, Any]) -> str:
    """Convert a submit_report payload into Slack mrkdwn.

    Shows the direct `answer` first, then appends only the optional sections that
    were actually populated. Falls back to the legacy `headline`/`summary` fields
    if `answer` is absent (backwards compatibility)."""
    severity = (data.get("severity") or "").lower()
    emoji = SEVERITY_EMOJI.get(severity, "")

    # Primary answer (new precise format). Fall back to legacy fields.
    answer = (data.get("answer") or "").strip()
    if not answer:
        headline = (data.get("headline") or "").strip()
        summary = (data.get("summary") or "").strip()
        answer = "\n\n".join(p for p in (headline, summary) if p).strip()

    key_facts: list[str] = data.get("key_facts") or []
    timeline: list[str] = data.get("timeline") or []
    personnel: list[str] = data.get("personnel") or []
    root_cause = (data.get("root_cause") or "").strip()
    next_steps: list[str] = data.get("next_steps") or []
    links: list[str] = data.get("links") or []

    lines: list[str] = []

    if answer:
        lines.append(f"{emoji} {answer}".strip())
    elif emoji:
        lines.append(emoji)

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
        lines.append("*People involved*")
        for person in personnel:
            lines.append(f"• {person}")

    # root_cause is legacy; only show if the answer didn't already cover it
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

    return "\n".join(lines).strip()
