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
        """Fetch a Freshservice ticket by numeric id. Returns the ticket dict."""
        url = f"{self._base()}/tickets/{ticket_id}"
        try:
            resp = httpx.get(url, headers=self._headers(), timeout=_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            return resp.json().get("ticket") or {}
        except httpx.HTTPStatusError as exc:
            logger.warning("Freshservice ticket %s HTTP %s", ticket_id, exc.response.status_code)
            return {"error": f"HTTP {exc.response.status_code}", "ticket_id": ticket_id}
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

        Returns a structured dict containing all these sections ready for LLM reasoning.
        """
        import re as _re

        ticket = self.get_ticket(ticket_id)
        if "error" in ticket:
            return ticket

        conversations = self.get_ticket_conversations(ticket_id, limit=50)
        activities = self.get_ticket_activities(ticket_id)

        cf = ticket.get("custom_fields") or {}

        # Extract timeline from conversations (strip HTML)
        timeline_entries: list[dict] = []
        for conv in conversations:
            body_html = conv.get("body") or ""
            body_text = _re.sub(r"<[^>]+>", " ", body_html)
            body_text = _re.sub(r"\s{2,}", " ", body_text).strip()
            if body_text:
                timeline_entries.append({
                    "id": conv.get("id"),
                    "created_at": conv.get("created_at"),
                    "private": conv.get("private"),
                    "text": body_text[:4000],
                })

        # PIR status from activities
        pir_status = None
        pir_number = None
        for act in activities:
            content = act.get("content") or ""
            if "PIR" in content:
                m = _re.search(r"#(PIR-\d+)", content)
                if m:
                    pir_number = m.group(1)
                for sub in act.get("sub_contents") or []:
                    if "Published" in str(sub):
                        pir_status = "Published"
                    elif "Draft" in str(sub) and pir_status != "Published":
                        pir_status = "Draft"

        return {
            "ticket_id": ticket.get("id"),
            "pir_number": pir_number,
            "pir_status": pir_status or ("generated" if cf.get("pir_generated") else "not_generated"),
            "subject": ticket.get("subject"),
            "description": _re.sub(r"<[^>]+>", " ", ticket.get("description") or "").strip()[:500],
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
            # Full incident timeline (from conversations)
            "timeline": timeline_entries,
            "pir_url": f"https://{self.domain}/a/tickets/{ticket_id}/post-incident-report",
        }

    def search_tickets(
        self,
        query: str = "",
        ticket_type: str = "Incident",
        page: int = 1,
        per_page: int = 10,
    ) -> list[dict]:
        """Search tickets via Freshservice filter API."""
        params: dict[str, Any] = {
            "type": ticket_type,
            "page": page,
            "per_page": per_page,
        }
        if query:
            params["query"] = f'"{query}"'
        url = f"{self._base()}/tickets/filter"
        try:
            resp = httpx.get(url, headers=self._headers(), params=params, timeout=_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            return resp.json().get("tickets") or []
        except Exception as exc:
            logger.warning("Freshservice search_tickets: %s", exc)
            return []

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
