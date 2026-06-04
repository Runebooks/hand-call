"""Unified metrics query: direct Prometheus API or Haystack Grafana MCP."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from agents.prometheus.haystack_mcp_client import HaystackMcpClient, HaystackMcpError
from agents.prometheus.prom_client import PromAuthError, PromClient
from common.haystack_tenant import resolve_haystack_tenant

logger = logging.getLogger(__name__)


class MetricsBackend:
    def __init__(
        self,
        prom: Optional[PromClient],
        mcp: Optional[HaystackMcpClient],
        *,
        mode: str = "direct",
    ):
        self.prom = prom
        self.mcp = mcp
        self.mode = (mode or "direct").strip().lower()
        self._mcp_ok = False

    @classmethod
    def from_env(cls) -> "MetricsBackend":
        mode = os.environ.get("PROMETHEUS_QUERY_BACKEND", "direct").strip().lower()
        return cls(PromClient.from_env(), HaystackMcpClient.from_env(), mode=mode)

    @property
    def live_query_enabled(self) -> bool:
        if self.mode == "mcp":
            return self.mcp is not None and bool(self.mcp.token)
        if self.mode == "auto":
            if self.mcp and self.mcp.token:
                return True
        if self.prom and self.prom.live_query_enabled:
            return True
        return False

    def describe(self) -> str:
        parts = [f"backend={self.mode}"]
        if self.prom:
            parts.append(f"prom={self.prom.base_url}")
        if self.mcp:
            parts.append(f"mcp={self.mcp.base_url}")
        return ", ".join(parts)

    def startup_checks(self) -> None:
        if self.mcp:
            self._mcp_ok = self.mcp.ping()
            if self._mcp_ok:
                logger.info("Haystack MCP reachable: %s", self.mcp.base_url)
            else:
                logger.warning(
                    "Haystack MCP not authorized or unreachable at %s "
                    "(using direct Prom API; set PROMETHEUS_QUERY_BACKEND=direct to silence)",
                    self.mcp.base_url,
                )
        if self.prom and self.prom.live_query_enabled:
            try:
                self.prom.health()
                logger.info("Prometheus API reachable: %s", self.prom.base_url)
            except PromAuthError as exc:
                logger.warning("Prometheus SSO/auth: %s", exc)
            except Exception:
                try:
                    self.prom.query("up")
                    logger.info("Prometheus query API reachable (up)")
                except Exception as exc:
                    logger.warning("Prometheus probe failed: %s", exc)

    def _use_mcp_for_query(self) -> bool:
        if not self.mcp or not self.mcp.token:
            return False
        if self.mode == "mcp":
            return True
        # auto: only call MCP when startup ping succeeded (avoids 401 on every query)
        return self.mode == "auto" and self._mcp_ok

    def query(
        self,
        promql: str,
        meta: Optional[dict[str, Any]] = None,
    ) -> tuple[dict[str, Any], str]:
        """Run PromQL; returns (payload, source_label)."""
        org_id = resolve_haystack_tenant(meta or {})
        if self._use_mcp_for_query():
            try:
                return self.mcp.query(promql), "Haystack MCP"
            except HaystackMcpError:
                if self.mode == "mcp":
                    raise
                logger.info("MCP query failed, falling back to direct Prom API")
            except Exception as exc:
                if self.mode == "mcp":
                    raise HaystackMcpError(str(exc)) from exc
                logger.info("MCP query failed (%s), falling back to direct Prom API", exc)

        if not self.prom or not self.prom.live_query_enabled:
            raise RuntimeError("No live metrics backend configured")
        return self.prom.query(promql, org_id=org_id), "Prometheus API"

    def format_result(
        self,
        payload: dict[str, Any],
        *,
        source: str,
        promql: str = "",
        summarize: bool = False,
    ) -> str:
        if source == "Haystack MCP":
            return HaystackMcpClient.format_result(payload)
        return PromClient.format_instant_result(
            payload, promql=promql, summarize=summarize
        )
