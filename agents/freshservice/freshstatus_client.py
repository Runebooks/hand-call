"""
Freshstatus public incidents API client (no auth required).

API: https://public-api.freshstatus.io/v1/public-incidents
account_id 65 = Freshworks.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://public-api.freshstatus.io/v1"
_TIMEOUT = 15.0
_DEFAULT_ACCOUNT_ID = "65"


class FreshstatusClient:
    def __init__(self, account_id: str | None = None):
        self.account_id = (
            account_id or os.environ.get("FRESHSTATUS_ACCOUNT_ID", _DEFAULT_ACCOUNT_ID)
        ).strip()

    def get_incidents(self) -> dict[str, Any]:
        """Return raw Freshstatus incidents payload (active + recent)."""
        try:
            resp = httpx.get(
                f"{_BASE}/public-incidents/",
                params={"account_id": self.account_id},
                timeout=_TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Freshstatus fetch failed: %s", exc)
            return {"error": str(exc), "results": []}

    def get_active_incidents(self) -> list[dict]:
        """Return only currently active (unresolved) incidents, newest first."""
        payload = self.get_incidents()
        results = payload.get("results") or []
        active = [
            r for r in results
            if r and (r.get("end_time") is None)
        ]
        active.sort(key=lambda r: r.get("updated_at") or r.get("start_time") or "", reverse=True)
        return active

    def get_recent_incidents(self, limit: int = 20) -> list[dict]:
        """Return the N most recent incidents (active + resolved)."""
        payload = self.get_incidents()
        results = payload.get("results") or []
        results_sorted = sorted(
            results,
            key=lambda r: r.get("updated_at") or r.get("start_time") or "",
            reverse=True,
        )
        return results_sorted[:limit]
