"""Haystack Grafana MCP client (in-cluster VPC endpoint).

Uses the same FWSS token as Haystack telemetry. When MCP auth is not enabled for
the tenant, callers should fall back to PromClient (direct /api/prom).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

MCP_PROTOCOL = "2024-11-05"


class HaystackMcpError(RuntimeError):
    pass


class HaystackMcpClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        tenant: str = "fw-noc",
        timeout: float = 25.0,
        query_tool: str = "",
        datasource_uid: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.tenant = tenant.strip()
        self.timeout = timeout
        self.query_tool = query_tool.strip()
        self.datasource_uid = datasource_uid.strip()
        self._session_id: Optional[str] = None

    @classmethod
    def from_env(cls) -> Optional["HaystackMcpClient"]:
        url = os.environ.get("HAYSTACK_MCP_URL", "").strip()
        if not url:
            return None
        token = (
            os.environ.get("HAYSTACK_MCP_TOKEN", "").strip()
            or os.environ.get("PROMETHEUS_TOKEN", "").strip()
        )
        tenant = os.environ.get("HAYSTACK_MCP_TENANT", "fw-noc").strip()
        return cls(
            url,
            token=token,
            tenant=tenant,
            query_tool=os.environ.get("HAYSTACK_MCP_QUERY_TOOL", "").strip(),
            datasource_uid=os.environ.get("HAYSTACK_MCP_DATASOURCE_UID", "").strip(),
        )

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.tenant:
            headers["X-Scope-OrgID"] = self.tenant
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                self.base_url,
                json=payload,
                headers=self._headers(),
            )
        if resp.status_code == 401:
            raise HaystackMcpError(
                "Haystack MCP returned 401 — FWSS token may not be authorized for MCP. "
                "Ask observability to enable in-cluster MCP for tenant "
                f"{self.tenant!r}, or set PROMETHEUS_QUERY_BACKEND=direct."
            )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise HaystackMcpError(data["error"])
        result = data.get("result") or {}
        if isinstance(result, dict) and result.get("sessionId"):
            self._session_id = str(result["sessionId"])
        return result

    def ping(self) -> bool:
        """Return True if initialize succeeds (MCP reachable + auth OK)."""
        try:
            self._rpc(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "prometheus-agent", "version": "1.0"},
                },
            )
            return True
        except Exception as exc:
            logger.debug("Haystack MCP ping failed: %s", exc)
            return False

    def _resolve_query_tool(self) -> str:
        if self.query_tool:
            return self.query_tool
        try:
            tools = self._rpc("tools/list", {})
            names = {t.get("name") for t in (tools.get("tools") or []) if t.get("name")}
            for candidate in ("grafana_query_prometheus", "query_prometheus"):
                if candidate in names:
                    return candidate
        except Exception as exc:
            logger.debug("tools/list failed, using default query_prometheus: %s", exc)
        return "query_prometheus"

    def query(self, promql: str) -> dict[str, Any]:
        tool = self._resolve_query_tool()
        args: dict[str, Any] = {"query": promql}
        if self.datasource_uid:
            args["datasourceUid"] = self.datasource_uid
            args["datasource_uid"] = self.datasource_uid
        result = self._rpc(
            "tools/call",
            {"name": tool, "arguments": args},
        )
        return _parse_tool_result(result)

    @staticmethod
    def format_result(data: dict[str, Any], *, limit: int = 10) -> str:
        """Format MCP tool output like PromClient.format_instant_result."""
        if data.get("status") == "success" and "data" in data:
            from agents.prometheus.prom_client import PromClient

            return PromClient.format_instant_result(data, limit=limit)
        if isinstance(data.get("result"), list):
            lines = ["**Live PromQL result (via Haystack MCP)**\n"]
            for item in data["result"][:limit]:
                lines.append(f"- {item}")
            return "\n".join(lines)
        text = data.get("text") or data.get("content") or json.dumps(data)[:2000]
        return f"**Live PromQL result (via Haystack MCP)**\n\n{text}"


def _parse_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize tools/call payload to Prometheus-style JSON when possible."""
    content = result.get("content") or []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not text:
            continue
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("status") == "success":
                return parsed
        except json.JSONDecodeError:
            return {"text": text}
    if result.get("structuredContent"):
        sc = result["structuredContent"]
        if isinstance(sc, dict):
            return sc
    return result
