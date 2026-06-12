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

    def get_ticket_conversations(self, ticket_id: int | str, limit: int = 5) -> list[dict]:
        """Fetch recent conversations/notes for a ticket."""
        url = f"{self._base()}/tickets/{ticket_id}/conversations"
        try:
            resp = httpx.get(url, headers=self._headers(), timeout=_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            convs = resp.json().get("conversations") or []
            return convs[-limit:]
        except Exception as exc:
            logger.warning("Freshservice conversations %s: %s", ticket_id, exc)
            return []

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
