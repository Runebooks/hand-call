"""
Prometheus A2A Agent — Haystack / Trigmetry metrics (read-only).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.a2a_server import A2AServer
from common.models import Artifact, Task

from agents.prometheus.prom_client import (
    PromAuthError,
    PromClient,
    build_hostname_promql,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

CARD_PATH = Path(__file__).parent / "agent_card.json"
DEFAULT_PORT = int(os.environ.get("A2A_PORT", "8080"))


class PrometheusAgent(A2AServer):
    def __init__(self, **kwargs):
        super().__init__(agent_card_path=str(CARD_PATH), **kwargs)
        self.prom = PromClient.from_env()

    async def on_startup(self) -> None:
        if not self.prom:
            logger.info("PROMETHEUS_URL unset — answers use alert metadata only")
            return
        logger.info("Prometheus URL configured: %s", self.prom.base_url)
        if not self.prom.live_query_enabled:
            logger.info(
                "Live PromQL disabled (public URL / no token). "
                "Set PROMETHEUS_TOKEN or PROMETHEUS_INTERNAL=true for live queries."
            )
            return
        try:
            self.prom.health()
            logger.info("Prometheus API reachable for live queries")
        except PromAuthError as exc:
            logger.warning("Prometheus SSO/auth: %s", exc)
        except Exception as exc:
            logger.warning("Prometheus health check failed: %s", exc)

    async def process_task(self, task: Task) -> Task:
        query = task.message.get_text() if task.message else ""
        meta = task.metadata or {}
        try:
            answer = self._handle_query(query, meta)
            task.add_artifact(Artifact.text(answer, name="prometheus-result"))
            task.mark_completed()
        except Exception as exc:
            logger.exception("Prometheus query failed")
            task.mark_failed(str(exc))
            task.add_artifact(Artifact.text(f"Prometheus agent error: {exc}"))
        return task

    def _handle_query(self, query: str, meta: dict[str, Any]) -> str:
        user = _user_question(query)
        lines = ["**Prometheus / metrics**", ""]

        rpm_block = _format_rpm_vs_threshold(meta, user)
        if rpm_block:
            lines.append(rpm_block)
            lines.append("")

        ctx = _format_alert_metric_context(meta)
        if ctx:
            lines.append(ctx)
            lines.append("")

        explicit = _extract_promql(user)
        if explicit and self.prom and self.prom.live_query_enabled:
            try:
                payload = self.prom.query(explicit)
                lines.append(f"**PromQL:** `{explicit}`\n")
                lines.append(PromClient.format_instant_result(payload))
                return "\n".join(lines)
            except Exception as exc:
                lines.append(f"_Live PromQL failed: {exc}_\n")

        if self.prom and self.prom.live_query_enabled and _wants_live_query(user, meta):
            promql = build_hostname_promql(meta)
            if promql:
                try:
                    payload = self.prom.query(promql)
                    lines.append(f"**Live query:** `{promql}`\n")
                    lines.append(PromClient.format_instant_result(payload))
                    return "\n".join(lines)
                except PromAuthError as exc:
                    lines.append(f"_Live PromQL skipped: {exc}_\n")
                except Exception as exc:
                    lines.append(f"_Live PromQL failed: {exc}_\n")
        elif self.prom and _wants_live_query(user, meta):
            lines.append(
                "_Live PromQL not configured._ `https://metrics.haystack.es` is the **Grafana UI** "
                "(Google SSO). For live metrics, set one of:\n"
                "- `PROMETHEUS_URL` → in-cluster Prometheus API (e.g. `http://prometheus…:9090`)\n"
                "- `PROMETHEUS_TOKEN` → bearer token for authenticated API\n"
                "- `PROMQL_HOSTNAME_QUERY` → your Freshdesk RPM metric query\n"
            )

        dashboard = meta.get("dashboard") or meta.get("dashboard1")
        if dashboard and "rpm" in user.lower():
            lines.append(f"<{dashboard}|Open Haystack dashboard for live charts>")

        return "\n".join(lines)


def _user_question(query: str) -> str:
    marker = "\n\nKubernetes context:"
    if marker in query:
        return query.split(marker, 1)[0].strip()
    return query.strip()


def _parse_number(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _format_rpm_vs_threshold(meta: dict[str, Any], user: str) -> str:
    """Answer RPM vs threshold from Trigmetry alert fields (authoritative at alert time)."""
    current = _parse_number(meta.get("current_value") or meta.get("fields", {}).get("current_value"))
    threshold = _parse_number(meta.get("threshold") or meta.get("fields", {}).get("threshold"))
    if current is None or threshold is None:
        return ""

    delta = current - threshold
    if delta > 0:
        status = f"**ABOVE threshold** by **{delta:g}** ({current:g} > {threshold:g})"
        emoji = ":red_circle:"
    elif delta < 0:
        status = f"**below threshold** by **{abs(delta):g}** ({current:g} < {threshold:g})"
        emoji = ":large_green_circle:"
    else:
        status = f"**at threshold** ({current:g} = {threshold:g})"
        emoji = ":large_yellow_circle:"

    summary = meta.get("summary") or meta.get("fields", {}).get("summary") or ""
    hostname = meta.get("hostname") or meta.get("alert_sre_attributes") or ""
    product = meta.get("product") or ""

    lines = [
        f"**RPM vs threshold** {emoji}",
        f"- **Current value:** `{current:g}`",
        f"- **Threshold:** `{threshold:g}`",
        f"- **Comparison:** {status}",
    ]
    if product:
        lines.append(f"- **Product:** `{product}`")
    if hostname:
        lines.append(f"- **Host:** `{hostname}`")
    if summary:
        lines.append(f"- **Summary:** {summary}")
    return "\n".join(lines)


def _extract_promql(text: str) -> str | None:
    match = re.search(r"(?:promql|query)\s*[:=]\s*[`'\"]?([^`\"'\n]+)", text, re.I)
    if match:
        return match.group(1).strip()
    if text.strip().startswith("{") or text.strip().startswith("sum("):
        return text.strip()
    return None


def _wants_live_query(text: str, meta: dict[str, Any]) -> bool:
    if re.search(
        r"\b(rpm|metric|metrics|prometheus|promql|current.?value|threshold|latency|"
        r"cpu|memory|haystack|dashboard|spike|rate)\b",
        text,
        re.I,
    ):
        return True
    return bool(meta.get("current_value") or meta.get("threshold"))


def _format_alert_metric_context(meta: dict[str, Any]) -> str:
    parts: list[str] = []
    skip = {"current_value", "threshold", "summary"}
    for key in (
        "alertname",
        "product",
        "hostname",
        "cluster",
        "region",
        "severity",
        "priority",
    ):
        val = meta.get(key) or meta.get("fields", {}).get(key)
        if val:
            parts.append(f"- **{key}:** `{val}`")
    for key in ("current_value", "threshold", "summary"):
        val = meta.get(key) or meta.get("fields", {}).get(key)
        if val and key not in skip:
            parts.append(f"- **{key}:** `{val}`")
    if not parts:
        return ""
    return "**Alert details**\n" + "\n".join(parts)


def main() -> None:
    host = os.environ.get("A2A_HOST", "0.0.0.0")
    port = int(os.environ.get("A2A_PORT", str(DEFAULT_PORT)))
    PrometheusAgent(host=host, port=port).run()


if __name__ == "__main__":
    main()
