"""Read-only Prometheus HTTP API client (Prometheus / Haystack)."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)


class PromAuthError(RuntimeError):
    """Prometheus URL requires SSO or missing credentials."""


class PromClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 20.0,
        verify_ssl: bool = True,
        follow_redirects: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.follow_redirects = follow_redirects

    @classmethod
    def from_env(cls) -> Optional["PromClient"]:
        url = os.environ.get("PROMETHEUS_URL", "").strip().rstrip("/")
        if not url:
            return None
        verify = os.environ.get("PROMETHEUS_VERIFY_SSL", "true").lower() not in (
            "0",
            "false",
            "no",
        )
        follow = os.environ.get("PROMETHEUS_FOLLOW_REDIRECTS", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        return cls(
            url,
            token=os.environ.get("PROMETHEUS_TOKEN", ""),
            verify_ssl=verify,
            follow_redirects=follow,
        )

    @property
    def live_query_enabled(self) -> bool:
        """Public Haystack/Grafana URLs need a token or in-cluster Prom URL."""
        if os.environ.get("PROMETHEUS_FORCE_LIVE", "").lower() in ("1", "true", "yes"):
            return True
        if self.token:
            return True
        if os.environ.get("PROMETHEUS_INTERNAL", "").lower() in ("1", "true", "yes"):
            return True
        # In-cluster / plain HTTP endpoints (no browser SSO)
        if self.base_url.startswith("http://"):
            return True
        return False

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _check_response(resp: httpx.Response) -> None:
        if resp.status_code in (301, 302, 303, 307, 308):
            location = (resp.headers.get("location") or "").lower()
            if any(x in location for x in ("oauth", "accounts.google", "login", "idpresponse")):
                raise PromAuthError(
                    "Prometheus URL is behind SSO (redirect to login). "
                    "Use an in-cluster Prometheus API URL, or set PROMETHEUS_TOKEN "
                    "for authenticated API access — the public Grafana URL "
                    "https://metrics.haystack.es is not a direct Prom API."
                )
            raise RuntimeError(
                f"Unexpected redirect ({resp.status_code}) to {resp.headers.get('location', '?')}"
            )
        resp.raise_for_status()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        with httpx.Client(
            timeout=self.timeout,
            verify=self.verify_ssl,
            follow_redirects=self.follow_redirects,
        ) as client:
            resp = client.get(url, params=params or {}, headers=self._headers())
            self._check_response(resp)
            if "application/json" not in (resp.headers.get("content-type") or ""):
                raise RuntimeError(
                    "Response is not JSON — check PROMETHEUS_URL points to Prometheus "
                    "/api/v1, not the Grafana UI."
                )
            payload = resp.json()
        if payload.get("status") != "success":
            raise RuntimeError(
                payload.get("error") or payload.get("errorType") or "Prometheus error"
            )
        return payload

    def health(self) -> dict[str, Any]:
        return self._get("/api/v1/status/config")

    def query(self, promql: str, *, time: Optional[str] = None) -> dict[str, Any]:
        params: dict[str, Any] = {"query": promql}
        if time:
            params["time"] = time
        return self._get("/api/v1/query", params)

    def query_range(
        self,
        promql: str,
        *,
        start: str,
        end: str,
        step: str = "60s",
    ) -> dict[str, Any]:
        return self._get(
            "/api/v1/query_range",
            {"query": promql, "start": start, "end": end, "step": step},
        )

    @staticmethod
    def format_instant_result(payload: dict[str, Any], *, limit: int = 10) -> str:
        data = payload.get("data", {})
        result_type = data.get("resultType", "")
        results = data.get("result") or []
        if not results:
            return "_No series returned._"
        lines = [f"**Live PromQL result** ({result_type}, {len(results)} series)\n"]
        for item in results[:limit]:
            metric = item.get("metric") or {}
            labels = ", ".join(f"{k}={v}" for k, v in sorted(metric.items())[:8])
            value = item.get("value", [None, None])[1]
            lines.append(f"- `{labels}` → **{value}**")
        if len(results) > limit:
            lines.append(f"\n_…and {len(results) - limit} more series_")
        return "\n".join(lines)


def build_hostname_promql(meta: dict[str, Any]) -> Optional[str]:
    """Best-effort PromQL from NOC alert metadata (override via PROMQL_HOSTNAME_QUERY)."""
    template = os.environ.get("PROMQL_HOSTNAME_QUERY", "").strip()
    hostname = (meta.get("hostname") or meta.get("alert_sre_attributes") or "").strip()
    product = (meta.get("product") or "").strip()
    cluster = (meta.get("cluster") or "").strip()
    region = (meta.get("region") or "").strip()
    if template:
        return template.format(
            hostname=hostname,
            product=product,
            cluster=cluster,
            region=region,
        )
    if not hostname:
        return None
    return f'{{hostname="{hostname}"}}'
