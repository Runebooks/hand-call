"""Read-only Prometheus HTTP API client (Prometheus / Haystack)."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

from common.haystack_tenant import is_freshdesk_app_id, is_haystack_tenant

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

    def _headers(self, org_id: Optional[str] = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        org = (org_id or os.environ.get("PROMETHEUS_ORG_ID", "")).strip()
        if org:
            headers["X-Scope-OrgID"] = org
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

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        org_id: Optional[str] = None,
    ) -> dict[str, Any]:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        with httpx.Client(
            timeout=self.timeout,
            verify=self.verify_ssl,
            follow_redirects=self.follow_redirects,
        ) as client:
            resp = client.get(url, params=params or {}, headers=self._headers(org_id))
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

    def health(self, *, org_id: Optional[str] = None) -> dict[str, Any]:
        return self._get("/api/v1/status/config", org_id=org_id)

    def query(
        self,
        promql: str,
        *,
        time: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"query": promql}
        if time:
            params["time"] = time
        return self._get("/api/v1/query", params, org_id=org_id)

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

    # Prefer these when summarizing noisy pod selector queries for Slack
    _POD_SUMMARY_METRICS = (
        "kube_pod_container_status_waiting_reason",
        "kube_pod_container_status_waiting",
        "kube_pod_container_status_restarts_total",
        "kube_pod_status_ready",
        "kube_pod_status_phase",
        "kube_pod_container_status_last_terminated_reason",
        "container_cpu_usage_seconds_total",
        "container_memory_usage_bytes",
    )

    @classmethod
    def format_instant_result(
        cls,
        payload: dict[str, Any],
        *,
        limit: int = 10,
        promql: str = "",
        summarize: bool = False,
    ) -> str:
        data = payload.get("data", {})
        result_type = data.get("resultType", "")
        results = data.get("result") or []
        if not results:
            hint = cls._empty_query_hint(promql)
            return "_No series returned._" + (f"\n\n{hint}" if hint else "")

        if summarize and len(results) > limit:
            results = cls._prioritize_pod_metrics(results)

        lines = [f"**Live PromQL result** ({result_type}, {len(results)} series)\n"]
        shown = results[:limit]
        for item in shown:
            metric = item.get("metric") or {}
            name = metric.get("__name__", "")
            short_labels = ", ".join(
                f"{k}={v}"
                for k, v in sorted(metric.items())
                if k in ("pod", "namespace", "container", "reason", "condition", "phase")
            )
            labels = short_labels or ", ".join(
                f"{k}={v}" for k, v in sorted(metric.items())[:6]
            )
            value = item.get("value", [None, None])[1]
            prefix = f"`{name}`" if name else "`series`"
            lines.append(f"- {prefix} ({labels}) → **{value}**")
        if len(results) > limit:
            lines.append(f"\n_…and {len(results) - limit} more series_")
        return "\n".join(lines)

    @classmethod
    def _prioritize_pod_metrics(cls, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        priority = {n: i for i, n in enumerate(cls._POD_SUMMARY_METRICS)}

        def sort_key(item: dict[str, Any]) -> tuple[int, str]:
            name = (item.get("metric") or {}).get("__name__", "")
            return (priority.get(name, 99), name)

        return sorted(results, key=sort_key)

    @staticmethod
    def _empty_query_hint(promql: str) -> str:
        q = promql.lower()
        if "up{" in q and "namespace=" in q:
            return (
                "_Hint: metric `up` in **fw-noc** has `k8s_cluster_name` but usually "
                "**no `namespace` label**. Try `up{k8s_cluster_name=\"n8n-prod\"}` or "
                "pod metrics like `kube_pod_container_status_waiting_reason{pod=\"…\",namespace=\"…\"}`._"
            )
        return ""


def build_live_promql(meta: dict[str, Any]) -> Optional[str]:
    """PromQL from alert metadata — RPM host ids, K8s pod/cluster, or PROMQL_HOSTNAME_QUERY."""
    template = os.environ.get("PROMQL_HOSTNAME_QUERY", "").strip()
    hostname = (meta.get("hostname") or "").strip()
    pod = (meta.get("pod") or meta.get("alert_sre_attributes") or "").strip()
    namespace = (meta.get("namespace") or "").strip()
    cluster = (meta.get("cluster") or "").strip()
    product = (meta.get("product") or "").strip()
    region = (meta.get("region") or "").strip()

    if template:
        return template.format(
            hostname=hostname,
            product=product,
            cluster=cluster,
            region=region,
            pod=pod,
            namespace=namespace,
        )

    # Haystack tenant (e.g. fw-noc) — org header, not a Prom label
    if hostname and is_haystack_tenant(hostname):
        hostname = ""
    # Trigmetry Freshdesk app id — not a Prometheus hostname label
    if hostname and not is_freshdesk_app_id(hostname):
        return f'{{hostname="{hostname}"}}'

    labels: list[str] = []
    if pod and not is_freshdesk_app_id(pod) and not is_haystack_tenant(pod):
        labels.append(f'pod="{pod}"')
    if namespace:
        labels.append(f'namespace="{namespace}"')
    if cluster:
        labels.append(f'k8s_cluster_name="{cluster}"')
    if labels:
        # Pod-scoped selector (many series); NL queries summarize key kube-state metrics
        return "{" + ",".join(labels) + "}"

    if hostname and (is_freshdesk_app_id(hostname) or is_haystack_tenant(hostname)):
        return None
    return None


def build_hostname_promql(meta: dict[str, Any]) -> Optional[str]:
    """Alias for build_live_promql (backward compatible)."""
    return build_live_promql(meta)
