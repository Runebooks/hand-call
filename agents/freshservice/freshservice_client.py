"""
Freshservice REST API client (read-only).

Auth: HTTP Basic, username = FRESHSERVICE_API_KEY, password = "X".
The API key is loaded from env only; never log or print it.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0


class FreshserviceUnavailable(Exception):
    """Raised when the Freshservice API is rate-limited (429) or unavailable (5xx/connect).

    Callers can catch this to fall back to the local Postgres MIM_Store cache.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _raise_if_unavailable(exc: httpx.HTTPStatusError) -> None:
    """Re-raise HTTP 429 / 5xx as FreshserviceUnavailable so callers can fall back."""
    code = exc.response.status_code
    if code == 429 or code >= 500:
        raise FreshserviceUnavailable(f"Freshservice HTTP {code}", status_code=code) from exc


class FreshserviceClient:
    def __init__(
        self,
        domain: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.domain = (domain or os.environ.get("FRESHSERVICE_DOMAIN", "")).strip().rstrip("/")
        self._api_key = (api_key or os.environ.get("FRESHSERVICE_API_KEY", "")).strip()
        if not self.domain:
            self.domain = "freshworks.freshservice.com"

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        cred = base64.b64encode(f"{self._api_key}:X".encode()).decode()
        return {
            "Authorization": f"Basic {cred}",
            "Content-Type": "application/json",
        }

    def _base(self) -> str:
        return f"https://{self.domain}/api/v2"

    def get_ticket(self, ticket_id: int | str) -> dict[str, Any]:
        """Fetch a Freshservice ticket by numeric id. Returns the ticket dict.

        Raises FreshserviceUnavailable on 429/5xx/connect errors so callers can
        fall back to the local Postgres MIM_Store cache.
        """
        url = f"{self._base()}/tickets/{ticket_id}"
        try:
            resp = httpx.get(url, headers=self._headers(), timeout=_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            return resp.json().get("ticket") or {}
        except httpx.HTTPStatusError as exc:
            _raise_if_unavailable(exc)
            logger.warning("Freshservice ticket %s HTTP %s", ticket_id, exc.response.status_code)
            return {"error": f"HTTP {exc.response.status_code}", "ticket_id": ticket_id}
        except (httpx.ConnectError, httpx.TimeoutException, httpx.TransportError) as exc:
            raise FreshserviceUnavailable(f"Freshservice connect error: {exc}") from exc
        except Exception as exc:
            logger.warning("Freshservice get_ticket %s: %s", ticket_id, exc)
            return {"error": str(exc), "ticket_id": ticket_id}

    def get_ticket_conversations(self, ticket_id: int | str, limit: int = 50) -> list[dict]:
        """Fetch all conversations/notes for a ticket (up to limit)."""
        url = f"{self._base()}/tickets/{ticket_id}/conversations"
        try:
            resp = httpx.get(url, headers=self._headers(), timeout=_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            convs = resp.json().get("conversations") or []
            return convs[-limit:]
        except Exception as exc:
            logger.warning("Freshservice conversations %s: %s", ticket_id, exc)
            return []

    def get_ticket_activities(self, ticket_id: int | str) -> list[dict]:
        """Fetch activity log for a ticket (who changed what, PIR status updates)."""
        url = f"{self._base()}/tickets/{ticket_id}/activities"
        try:
            resp = httpx.get(url, headers=self._headers(), timeout=_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            return resp.json().get("activities") or []
        except Exception as exc:
            logger.warning("Freshservice activities %s: %s", ticket_id, exc)
            return []

    def get_pir(self, ticket_id: int | str) -> dict[str, Any]:
        """
        Assemble the Post Incident Report (PIR) for a MIM ticket.

        Freshservice does not expose a single /post_incident_report REST endpoint.
        The PIR data is distributed across:
          - ticket.custom_fields  (MTTA, MTTD, MTTR, timestamps, impact, product)
          - ticket.conversations  (incident timeline, bridge updates, Slack thread)
          - ticket.activities     (PIR status audit: Draft → Published)
          - ticket fields         (subject, description, status, priority, assignee)

        Returns a structured dict with parsed timeline_events, personnel list,
        and raw_bridge_notes for full LLM reasoning.
        """
        import re as _re

        ticket = self.get_ticket(ticket_id)
        if "error" in ticket:
            return ticket

        conversations = self.get_ticket_conversations(ticket_id, limit=50)
        activities = self.get_ticket_activities(ticket_id)

        cf = ticket.get("custom_fields") or {}

        # Bridge notes use format: "DD/MM/YYYY HH:MM AM/PM <action>"
        _TS_RE = _re.compile(r"\d{1,2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}\s*[AP]M")
        # Non-name words to skip when extracting personnel hints
        _SKIP_NAMES = {
            "The", "This", "Incident", "Status", "Slack", "Zoom", "NOC",
            "Haystack", "Freshdesk", "Freshservice", "Freshworks", "Freshchat",
            "India", "Rollback", "Start", "End", "Time", "UTC", "IST",
            "Customer", "Team", "Bridge", "Call", "MIM", "PIR", "RCA",
        }
        # Broad name pattern: first name (capitalized) + 1-3 surname tokens (any case after initial)
        # Handles: "Sarvani Jammalamadaka", "Akhilesh R", "Dhanasekar S R", "Pravien SK"
        _NAME_RE = _re.compile(
            r"\b([A-Z][a-z]{1,20}(?:\s[A-Z][A-Za-z]{0,20}){1,3})\b"
        )

        timeline_events: list[dict] = []
        all_names: set[str] = set()
        raw_bridge_notes: list[str] = []
        pir_narrative_parts: list[str] = []

        for conv in sorted(conversations, key=lambda c: c.get("created_at") or ""):
            body_html = conv.get("body") or ""
            body_text = _re.sub(r"<[^>]+>", " ", body_html)
            body_text = _re.sub(r"&nbsp;", " ", body_text)
            body_text = _re.sub(r"\s{2,}", " ", body_text).strip()
            if not body_text:
                continue

            raw_bridge_notes.append(body_text[:8000])
            pir_narrative_parts.append(body_text)

            stamps = _TS_RE.findall(body_text)
            parts = _TS_RE.split(body_text)

            if stamps:
                for i, stamp in enumerate(stamps):
                    raw_event = parts[i + 1].strip() if (i + 1) < len(parts) else ""
                    if not raw_event:
                        continue
                    event_text = raw_event.split("\n")[0].strip()
                    if event_text:
                        timeline_events.append({"time": stamp, "event": event_text[:400]})
                    # Broad name extraction: captures single-initial surnames too
                    for m in _NAME_RE.finditer(raw_event[:400]):
                        name = m.group(1).strip()
                        # Must have at least 2 tokens, first token not a skip word
                        tokens = name.split()
                        if len(tokens) < 2:
                            continue
                        if tokens[0] in _SKIP_NAMES:
                            continue
                        # Accept if: multi-letter surname OR single-letter uppercase initial
                        # e.g. "Akhilesh R" -> tokens = ["Akhilesh", "R"] — R is uppercase len-1 OK
                        if len(tokens[-1]) >= 1 and tokens[-1][0].isupper():
                            all_names.add(name)
            else:
                timeline_events.append({
                    "time": conv.get("created_at", ""),
                    "event": body_text[:600],
                })

        # PIR status and number from activities
        pir_status = None
        pir_number = None
        for act in activities:
            content = act.get("content") or ""
            if "PIR" in content:
                m = _re.search(r"#?(PIR-\d+)", content)
                if m:
                    pir_number = m.group(1)
                for sub in act.get("sub_contents") or []:
                    s = str(sub)
                    if "Published" in s:
                        pir_status = "Published"
                    elif "Draft" in s and pir_status != "Published":
                        pir_status = "Draft"

        if not pir_status and cf.get("pir_generated"):
            pir_status = "Published"
        if not pir_number and cf.get("pir_number"):
            pir_number = str(cf["pir_number"])

        return {
            "ticket_id": ticket.get("id"),
            "pir_number": pir_number,
            "pir_status": pir_status or ("generated" if cf.get("pir_generated") else "not_generated"),
            "subject": ticket.get("subject"),
            "description": _re.sub(r"<[^>]+>", " ", ticket.get("description") or "").strip()[:800],
            "status": ticket.get("status"),
            "priority": ticket.get("priority"),
            "created_at": ticket.get("created_at"),
            "updated_at": ticket.get("updated_at"),
            # Operational metrics from custom fields
            "incident_start_time": cf.get("incident_start_time"),
            "incident_end_time": cf.get("incident_end_time"),
            "incident_detected_time": cf.get("incident_detected_time"),
            "incident_acknowledged_time": cf.get("incident_acknowledged_time"),
            "mtta_minutes": cf.get("time_to_ack_in_minutes_time_between_incident_occurrence_and_on_call_involvement"),
            "mttd_minutes": cf.get("time_to_detect_in_minutes_time_passed_between_the_onset_of_an_incident_and_its_discovery"),
            "mttr_minutes": cf.get("time_to_recover_in_minutes_time_passed_between_the_onset_of_the_incident_and_its_recovery_this_should_be_ideally_greater_than_the_time_to_detect"),
            # Impact / classification
            "product": cf.get("myproduct") or cf.get("product"),
            "module": cf.get("module"),
            "products_affected": cf.get("msf_products_affected") or [],
            "regions_affected": cf.get("msf_affected_regions") or [],
            "issue_category": cf.get("issue_category"),
            "major_incident_type": cf.get("major_incident_type"),
            "type_of_incident": cf.get("type_of_incident"),
            "impact_to_customer": cf.get("impact_to_customer"),
            "statuspage_url": cf.get("statuspage_url"),
            "status_page_updated": cf.get("status_page_updated"),
            "rca_presented_in_mom": cf.get("rca_presented_in_mom"),
            "assignee_manager": cf.get("assignee_manager"),
            # Parsed individual timeline events from bridge notes (chronological)
            "timeline_events": timeline_events,
            # All people mentioned by name in the incident bridge — who was involved
            # This is a best-effort hint; the LLM should derive the authoritative list from pir_narrative
            "personnel_hint": sorted(all_names),
            # Kept for backwards compat
            "personnel": sorted(all_names),
            # Full, cleaned, chronological bridge-note text — authoritative source for
            # LLM extraction of people, roles, and sequence of events
            "pir_narrative": "\n\n".join(pir_narrative_parts),
            # True if bridge notes are present (PIR data available for LLM reasoning)
            "pir_attached": bool(pir_narrative_parts),
            # Raw bridge note texts — LLM can reason over full narrative
            "raw_bridge_notes": raw_bridge_notes,
            "pir_url": f"https://{self.domain}/a/tickets/{ticket_id}/post-incident-report",
        }

    def list_major_incidents(
        self,
        product_filter: str = "",
        limit: int = 20,
        date_filter: str = "",
        issue_category: str = "",
        months_back: int = 0,
    ) -> list[dict]:
        """
        List recent Major Incident (MIM) tickets, newest first.
        Uses workspace_id=24 + type=Major Incident for server-side filtering.

        Client-side filters (all optional, combinable):
        - product_filter: case-insensitive substring on product/module/subject.
        - date_filter: a specific date ("May 14", "2026-05-14").
        - issue_category: Infra / Third-party / Code / Database / Deployment /
          Configuration (synonym-aware, case-insensitive).
        - months_back: keep only incidents whose start/created date is within the
          last N months.

        Raises FreshserviceUnavailable on 429/5xx/connect so callers can fall back
        to the Postgres MIM_Store cache.
        """
        import re as _dre
        from datetime import datetime, timedelta, timezone

        collected: list[dict] = []
        page = 1
        per_page = 30

        # ── date_filter parsing (single specific day) ───────────────────────────
        date_lower = date_filter.lower().strip() if date_filter else ""
        _MONTH_MAP = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04",
            "may": "05", "jun": "06", "jul": "07", "aug": "08",
            "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }
        date_prefix = ""
        if date_lower:
            m = _dre.search(r"(\d{4})-(\d{2})-(\d{2})", date_filter)
            if m:
                date_prefix = m.group(0)
            else:
                m2 = _dre.search(r"(\w{3,9})\s+(\d{1,2}),?\s*(\d{4})?", date_lower)
                if m2:
                    mon_word = m2.group(1)[:3]
                    day = m2.group(2).zfill(2)
                    yr = m2.group(3) or "2026"
                    mon_num = _MONTH_MAP.get(mon_word, "")
                    if mon_num:
                        date_prefix = f"{yr}-{mon_num}-{day}"

        # ── months_back window cutoff ────────────────────────────────────────────
        cutoff_iso = ""
        if months_back and months_back > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=30 * months_back)
            cutoff_iso = cutoff.strftime("%Y-%m-%d")

        # ── issue_category normalization (synonym-aware) ─────────────────────────
        _CAT_SYNONYMS = {
            "third-party": ["third-party", "third party", "thirdparty", "vendor", "external"],
            "infra": ["infra", "infrastructure"],
            "code": ["code", "bug", "defect", "regression"],
            "database": ["database", "db"],
            "deployment": ["deployment", "deploy", "release", "rollout"],
            "configuration": ["configuration", "config", "misconfig"],
        }
        cat_terms: list[str] = []
        if issue_category:
            ic_lower = issue_category.lower().strip()
            matched = False
            for _canon, syns in _CAT_SYNONYMS.items():
                if any(s in ic_lower for s in syns):
                    cat_terms = syns
                    matched = True
                    break
            if not matched:
                cat_terms = [ic_lower]

        prod_lower = product_filter.lower().strip()

        while len(collected) < limit:
            url = f"{self._base()}/tickets"
            params: dict[str, Any] = {
                "workspace_id": "24",
                "type": "Major Incident",
                "per_page": per_page,
                "page": page,
                "order_by": "created_at",
                "order_type": "desc",
            }
            try:
                resp = httpx.get(url, headers=self._headers(), params=params, timeout=_TIMEOUT, follow_redirects=True)
                resp.raise_for_status()
                tickets = resp.json().get("tickets") or []
            except httpx.HTTPStatusError as exc:
                _raise_if_unavailable(exc)
                logger.warning("Freshservice list_major_incidents page=%d HTTP %s", page, exc.response.status_code)
                break
            except (httpx.ConnectError, httpx.TimeoutException, httpx.TransportError) as exc:
                raise FreshserviceUnavailable(f"Freshservice connect error: {exc}") from exc
            except Exception as exc:
                logger.warning("Freshservice list_major_incidents page=%d: %s", page, exc)
                break

            if not tickets:
                break

            reached_cutoff = False
            for t in tickets:
                cf = t.get("custom_fields") or {}
                start = str(cf.get("incident_start_time") or t.get("created_at") or "")

                # months_back: tickets are newest-first; once older than cutoff, stop.
                if cutoff_iso and start and start[:10] < cutoff_iso:
                    reached_cutoff = True
                    break

                if prod_lower:
                    prod_blob = " ".join([
                        str(cf.get("myproduct") or ""),
                        str(cf.get("module") or ""),
                        " ".join(cf.get("msf_products_affected") or []),
                        str(t.get("subject") or ""),
                    ]).lower()
                    if prod_lower not in prod_blob:
                        continue
                if date_prefix and date_prefix not in start:
                    continue
                if cat_terms:
                    cat_val = str(cf.get("issue_category") or "").lower()
                    if not any(term in cat_val for term in cat_terms):
                        continue

                collected.append({
                    "id": t.get("id"),
                    "subject": t.get("subject"),
                    "status": t.get("status"),
                    "priority": t.get("priority"),
                    "created_at": t.get("created_at"),
                    "type": t.get("type"),
                    "product": cf.get("myproduct") or cf.get("product"),
                    "module": cf.get("module"),
                    "products_affected": cf.get("msf_products_affected") or [],
                    "regions_affected": cf.get("msf_affected_regions") or [],
                    "issue_category": cf.get("issue_category"),
                    "major_incident_type": cf.get("major_incident_type"),
                    "type_of_incident": cf.get("type_of_incident"),
                    "alert_generated_by": cf.get("alert_generated_by"),
                    "mtta_minutes": cf.get("time_to_ack_in_minutes_time_between_incident_occurrence_and_on_call_involvement"),
                    "mttd_minutes": cf.get("time_to_detect_in_minutes_time_passed_between_the_onset_of_an_incident_and_its_discovery"),
                    "mttr_minutes": cf.get("time_to_recover_in_minutes_time_passed_between_the_onset_of_the_incident_and_its_recovery_this_should_be_ideally_greater_than_the_time_to_detect"),
                    "incident_start_time": cf.get("incident_start_time"),
                    "incident_end_time": cf.get("incident_end_time"),
                    "impact_to_customer": (cf.get("impact_to_customer") or "")[:300],
                    "statuspage_url": cf.get("statuspage_url"),
                    "status_page_updated": cf.get("status_page_updated"),
                    "rca_presented_in_mom": cf.get("rca_presented_in_mom"),
                    "pir_generated": cf.get("pir_generated"),
                    "pir_url": f"https://{self.domain}/a/tickets/{t.get('id')}/post-incident-report",
                })
                if len(collected) >= limit:
                    break

            if reached_cutoff or len(tickets) < per_page:
                break
            page += 1

        return collected

    def search_tickets(
        self,
        query: str = "",
        ticket_type: str = "Incident",
        page: int = 1,
        per_page: int = 15,
        date_filter: str = "",
        issue_category: str = "",
        months_back: int = 0,
    ) -> list[dict]:
        """
        Search / list MIM tickets. When query is a product name (e.g. 'Freshdesk'),
        delegates to list_major_incidents for accurate results since the filter API
        does not support type= filtering.
        """
        return self.list_major_incidents(
            product_filter=query,
            limit=min(per_page, 30),
            date_filter=date_filter,
            issue_category=issue_category,
            months_back=months_back,
        )

    def get_analytics_export_csv(self, export_id: str) -> str:
        """Fetch a Freshservice Analytics scheduled-export CSV as raw text."""
        url = f"{self._base()}/analytics/export"
        params = {"id": export_id}
        try:
            resp = httpx.get(url, headers=self._headers(), params=params, timeout=60.0)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            logger.warning("Freshservice analytics export %s: %s", export_id, exc)
            return ""
