"""
Structured report schema + Slack renderer for the kubernetes-agent.

The LLM produces a structured report (via the ``submit_report`` tool or the
JSON-planner). ``render_report`` turns it into clean, consistent Slack markdown
that highlights the key data instead of dumping everything.

Long logs are emitted inside a sentinel fenced block::

    ```noc-logs:<label>
    ...full logs...
    ```

The Slack layer (``master/slack_thread.py``) detects these blocks and uploads
them as a thread file/snippet, keeping the chat message short and readable.
``extract_log_artifacts`` is the shared parser for that offload.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

LOG_SENTINEL_PREFIX = "noc-logs:"
CONFIRM_SENTINEL = "noc-confirm"

# Full logs longer than this (chars) are emitted as an offloadable artifact
# block; shorter excerpts stay inline as a normal code block.
INLINE_LOG_MAX_CHARS = 700

SEVERITY_EMOJI = {
    "critical": "\U0001f534",  # red circle
    "warning": "\U0001f7e0",  # orange circle
    "info": "\U0001f535",  # blue circle
    "ok": "\U0001f7e2",  # green circle
}

SUBMIT_REPORT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_report",
        "description": (
            "Provide the FINAL answer to the user as a structured report. Call this "
            "exactly once, after you have gathered the needed data with the other "
            "tools. Keep every field concise and highlight only what matters — do "
            "NOT paste large blobs into summary or facts. Put long logs in full_logs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "headline": {
                    "type": "string",
                    "description": "One short line naming the subject and outcome, e.g. 'checkout-api in CrashLoopBackOff: DB connection refused'.",
                },
                "severity": {
                    "type": "string",
                    "enum": ["critical", "warning", "info", "ok"],
                    "description": "Overall severity of the finding.",
                },
                "summary": {
                    "type": "string",
                    "description": "1-2 plain sentences summarizing the answer.",
                },
                "facts": {
                    "type": "array",
                    "description": "The key highlighted data points as label/value pairs (e.g. Namespace, Pod, Status, Restarts, Ready).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["label", "value"],
                    },
                },
                "root_cause": {
                    "type": "string",
                    "description": "The root cause, if identified.",
                },
                "log_excerpt": {
                    "type": "string",
                    "description": "Only the few most relevant log lines (<= 8 lines). Omit if logs are not relevant.",
                },
                "full_logs": {
                    "type": "string",
                    "description": "Complete/long logs to attach as a downloadable file. Omit unless logs are long.",
                },
                "logs_label": {
                    "type": "string",
                    "description": "Short name for the log file/section, e.g. 'checkout-api'.",
                },
                "next_steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recommended next checks or actions for the on-call.",
                },
                "can_do": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Follow-up actions the agent can perform on request (e.g. 'pull previous logs', 'restart the pod').",
                },
            },
            "required": ["headline", "summary"],
        },
    },
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def render_report(data: dict[str, Any]) -> str:
    """Render a structured report dict into clean Slack markdown."""
    if not isinstance(data, dict):
        return _clean(data) or "_No answer produced._"

    headline = _clean(data.get("headline"))
    severity = _clean(data.get("severity")).lower()
    summary = _clean(data.get("summary"))
    root_cause = _clean(data.get("root_cause"))
    log_excerpt = _clean(data.get("log_excerpt"))
    full_logs = _clean(data.get("full_logs"))
    logs_label = _clean(data.get("logs_label")) or "logs"

    emoji = SEVERITY_EMOJI.get(severity, "\U0001f50d")  # magnifying glass default
    lines: list[str] = []

    if headline:
        lines.append(f"{emoji} **{headline}**")
    if summary:
        lines.append("")
        lines.append(summary)

    facts = data.get("facts") or []
    fact_lines = []
    for fact in facts:
        if isinstance(fact, dict):
            label = _clean(fact.get("label"))
            value = _clean(fact.get("value"))
        else:
            label, value = "", _clean(fact)
        if not value:
            continue
        fact_lines.append(f"• **{label}:** {value}" if label else f"• {value}")
    if fact_lines:
        lines.append("")
        lines.append("**Key details**")
        lines.extend(fact_lines)

    if root_cause:
        lines.append("")
        lines.append("**Root cause**")
        lines.append(root_cause)

    # Logs: short excerpt inline; long full logs go to an offloadable artifact.
    inline_logs = log_excerpt
    offload_logs = ""
    if full_logs:
        if len(full_logs) > INLINE_LOG_MAX_CHARS:
            offload_logs = full_logs
            inline_logs = inline_logs or _head_tail(full_logs, lines_each=4)
        else:
            inline_logs = inline_logs or full_logs
    if inline_logs:
        lines.append("")
        lines.append(f"**Logs** (`{logs_label}`)")
        lines.append("```")
        lines.append(inline_logs.strip()[:1200])
        lines.append("```")

    next_steps = [_clean(s) for s in (data.get("next_steps") or []) if _clean(s)]
    if next_steps:
        lines.append("")
        lines.append("**Recommended next steps**")
        lines.extend(f"• {step}" for step in next_steps)

    can_do = [_clean(s) for s in (data.get("can_do") or []) if _clean(s)]
    if can_do:
        lines.append("")
        lines.append("_I can also:_ " + " · ".join(can_do))

    if offload_logs:
        lines.append("")
        lines.append(f"```{LOG_SENTINEL_PREFIX}{_safe_label(logs_label)}")
        lines.append(offload_logs.strip())
        lines.append("```")

    rendered = "\n".join(lines).strip()
    return rendered or summary or "_No answer produced._"


def _head_tail(text: str, lines_each: int = 4) -> str:
    rows = text.strip().splitlines()
    if len(rows) <= lines_each * 2:
        return text.strip()
    head = rows[:lines_each]
    tail = rows[-lines_each:]
    return "\n".join(head + ["… (truncated — full logs attached) …"] + tail)


def _safe_label(label: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label.strip()).strip("-")
    return slug or "logs"


_LOG_BLOCK_RE = re.compile(
    r"```" + re.escape(LOG_SENTINEL_PREFIX) + r"([^\n`]*)\n(.*?)```",
    re.DOTALL,
)


def extract_log_artifacts(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Pull sentinel ``noc-logs`` blocks out of a message.

    Returns ``(cleaned_text, [(label, content), ...])``. The cleaned text has
    each block replaced with a short reference line. Used by the Slack layer to
    upload the logs as a thread file/snippet.
    """
    artifacts: list[tuple[str, str]] = []

    def _replace(match: "re.Match[str]") -> str:
        label = (match.group(1) or "logs").strip() or "logs"
        content = (match.group(2) or "").strip()
        if not content:
            return ""
        artifacts.append((label, content))
        return f"_\U0001f4ce Full logs attached as_ `{label}.log`"

    cleaned = _LOG_BLOCK_RE.sub(_replace, text or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, artifacts


_CONFIRM_RE = re.compile(
    r"```" + re.escape(CONFIRM_SENTINEL) + r"\s*\n(.*?)```",
    re.DOTALL,
)


def confirm_marker(operation: str, summary: str) -> str:
    """Hidden marker that flags a message as a mutation confirmation prompt.

    The Slack layer detects this, strips it, and renders Approve/Cancel buttons.
    """
    payload = json.dumps({"op": operation or "", "summary": summary or ""})
    return f"```{CONFIRM_SENTINEL}\n{payload}\n```"


def extract_confirm(text: str) -> tuple[str, Optional[dict[str, Any]]]:
    """Pull a ``noc-confirm`` marker out of a message.

    Returns ``(cleaned_text, info)`` where ``info`` is ``{"op", "summary"}`` or
    ``None`` when the message is not a confirmation prompt.
    """
    match = _CONFIRM_RE.search(text or "")
    if not match:
        return text or "", None
    try:
        info = json.loads(match.group(1).strip())
    except (json.JSONDecodeError, TypeError):
        info = {"op": "", "summary": ""}
    cleaned = _CONFIRM_RE.sub("", text or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, info
