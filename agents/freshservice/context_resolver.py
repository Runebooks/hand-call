"""
Context resolution: parse incident references, identify ticket IDs to fetch,
and select the focused incident from thread history or explicit mention.

Ported from n8n Code: Resolve ticket & context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from agents.freshservice.products import ProductFilter, parse_product_filter

# MI-4301250 or plain 4301250 (6+ digits)
_MI_REF_RE = re.compile(r"\bMI-(\d{4,})\b|\b(\d{6,})\b", re.I)
# "incident 693" / "incident number 693" etc.
_INCIDENT_NO_RE = re.compile(r"\bincident\s*(?:no\.?|number|#)?\s*(\d{3,6})\b", re.I)


@dataclass
class ResolvedContext:
    ticket_ids: list[str] = field(default_factory=list)    # Freshservice numeric ids
    incident_no: Optional[str] = None                      # internal NOC incident_no
    focused_incident: Optional[dict] = None                # single Outages_data row if focused
    product_filter: Optional[ProductFilter] = None
    wants_incident_report: bool = False
    report_period: Optional[str] = None
    report_segment: Optional[str] = None


def resolve_context(
    user_text: str,
    thread_messages: list[dict] | None = None,
    db_rows: list[dict] | None = None,
) -> ResolvedContext:
    """
    Given the user's message plus optional thread/DB context, return:
    - ticket_ids: Freshservice ticket ids to fetch
    - incident_no: internal NOC incident number if mentioned
    - focused_incident: closest Outages_data row if in thread
    - product_filter: Freshworks product if named
    - wants_incident_report: True for period-count/analytics questions
    - report_period / report_segment: for analytics
    """
    ctx = ResolvedContext()
    lower = (user_text or "").lower()

    # Parse Freshservice ticket refs (MI-4301250 → "4301250")
    ticket_ids: set[str] = set()
    for m in _MI_REF_RE.finditer(user_text or ""):
        tid = m.group(1) or m.group(2)
        if tid:
            ticket_ids.add(tid)
    ctx.ticket_ids = sorted(ticket_ids)

    # Parse internal incident_no (3–5 digits like "693")
    m2 = _INCIDENT_NO_RE.search(user_text or "")
    if m2:
        ctx.incident_no = m2.group(1)

    # Product filter (e.g. "Freshdesk outages")
    ctx.product_filter = parse_product_filter(user_text or "")

    # Detect period-based analytics questions
    _period_words = (
        "how many", "count", "how often", "total", "in april", "in march",
        "q1", "q2", "q3", "q4", "this month", "last month", "this year",
        "last year", "trend", "analytics", "report", "stats",
    )
    _segment_words = ("due to", "caused by", "deployment", "cause segment", "segment")

    if any(w in lower for w in _period_words):
        ctx.wants_incident_report = True
        ctx.report_period = _extract_period(lower)

    if any(w in lower for w in _segment_words):
        ctx.wants_incident_report = True
        ctx.report_segment = _extract_segment(lower)

    # Try to find focused incident from thread history
    if thread_messages and db_rows:
        ctx.focused_incident = _find_focused_incident(thread_messages, db_rows)

    return ctx


def _extract_period(lower: str) -> Optional[str]:
    """Very lightweight period extractor for analytics queries."""
    months = {
        "jan": "January", "feb": "February", "mar": "March", "apr": "April",
        "may": "May", "jun": "June", "jul": "July", "aug": "August",
        "sep": "September", "oct": "October", "nov": "November", "dec": "December",
    }
    for abbr, full in months.items():
        if abbr in lower or full.lower() in lower:
            # try to pick year
            m = re.search(r"\b(202\d)\b", lower)
            year = m.group(1) if m else ""
            return f"{full} {year}".strip()
    for q in ("q1", "q2", "q3", "q4"):
        if q in lower:
            m = re.search(r"\b(202\d)\b", lower)
            year = m.group(1) if m else ""
            return f"{q.upper()} {year}".strip()
    if "this year" in lower:
        import datetime
        return str(datetime.date.today().year)
    if "last year" in lower:
        import datetime
        return str(datetime.date.today().year - 1)
    return None


def _extract_segment(lower: str) -> Optional[str]:
    segments = ["deployment", "infra", "database", "third-party", "external", "configuration"]
    for s in segments:
        if s in lower:
            return s
    return None


def _find_focused_incident(thread_messages: list[dict], db_rows: list[dict]) -> Optional[dict]:
    """
    Scan thread messages (newest first) for incident_no references and match to
    an Outages_data row.
    """
    for msg in reversed(thread_messages or []):
        text = msg.get("text") or ""
        m = re.search(r"\b(\d{3,6})\b", text)
        if m:
            candidate = m.group(1)
            for row in db_rows or []:
                inc = str(row.get("incident_no") or row.get("Incident_no") or "")
                if inc and inc == candidate:
                    return row
    # Fall back to the newest Outages_data row if no thread match
    for row in db_rows or []:
        if row.get("_mysql_source") == "Outages_data":
            return row
    return None


def mi_ref_to_id(ref: str) -> Optional[str]:
    """Convert 'MI-4301250' or '4301250' to the numeric string '4301250'."""
    ref = (ref or "").strip()
    m = re.match(r"(?:MI-)?(\d{4,})", ref, re.I)
    return m.group(1) if m else None
