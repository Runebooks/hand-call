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
from common.llm import LLMClient
from common.models import Artifact, Task

from agents.prometheus.metrics_backend import MetricsBackend
from common.haystack_tenant import is_haystack_tenant, resolve_haystack_tenant
from agents.prometheus.prom_client import PromAuthError, PromClient
from agents.prometheus.promql_planner import plan_promql
from agents.prometheus.slack_format import compact_slack_enabled, format_compact_response

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
        self.metrics = MetricsBackend.from_env()
        self.prom = self.metrics.prom
        self.llm = LLMClient()

    async def on_startup(self) -> None:
        if not self.metrics.prom and not self.metrics.mcp:
            logger.info("PROMETHEUS_URL and HAYSTACK_MCP_URL unset — alert metadata only")
            return
        logger.info("Metrics config: %s", self.metrics.describe())
        if os.environ.get("PROMETHEUS_ORG_ID", "").strip():
            logger.info("Prometheus org header: X-Scope-OrgID=%s", os.environ.get("PROMETHEUS_ORG_ID"))
        if self.llm.enabled():
            logger.info("PromQL LLM planning enabled (model=%s)", self.llm.model)
        else:
            logger.info("PromQL LLM planning disabled (%s)", self.llm.disabled_reason or "rules only")
        if not self.metrics.live_query_enabled:
            logger.info(
                "Live PromQL disabled. Set PROMETHEUS_TOKEN / in-cluster PROMETHEUS_URL "
                "or HAYSTACK_MCP_URL + HAYSTACK_MCP_TOKEN."
            )
            return
        self.metrics.startup_checks()
        if self.prom and self.prom.live_query_enabled:
            try:
                payload = self.prom.query("up")
                n = len(payload.get("data", {}).get("result") or [])
                logger.info("FWSS Prom API ready (%d series for up)", n)
            except Exception as exc:
                logger.warning("FWSS Prom API probe failed: %s", exc)

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
        meta = _apply_question_overrides(user, meta)
        rpm_block = _format_rpm_vs_threshold(meta, user)
        explicit = _extract_promql(user)

        if not self.metrics.live_query_enabled:
            return _metadata_only_answer(user, meta, rpm_block)

        prom_plan = plan_promql(
            user,
            meta,
            llm=self.llm,
            prom=self.prom,
            explicit=explicit,
        )

        if not prom_plan.queries and not rpm_block:
            return _metadata_only_answer(user, meta, rpm_block)

        query_results: list[tuple[str, dict[str, Any], str]] = []
        if prom_plan.queries:
            for promql in prom_plan.queries:
                try:
                    payload, source = self.metrics.query(promql, meta)
                    query_results.append((promql, payload, source))
                except PromAuthError as exc:
                    return _metadata_only_answer(
                        user, meta, rpm_block, extra=f"_Live PromQL skipped: {exc}_"
                    )
                except Exception as exc:
                    logger.warning("Query failed for %s: %s", promql, exc)

        if compact_slack_enabled() and not explicit:
            body = format_compact_response(
                user_question=user,
                meta=meta,
                plan=prom_plan,
                query_results=query_results,
                rpm_block=rpm_block,
                explicit_promql=False,
            )
            if body:
                return f"**Prometheus / metrics**\n\n{body}"

        return _verbose_answer(
            user, meta, rpm_block, explicit, prom_plan, query_results, self.metrics
        )


def _metadata_only_answer(
    user: str,
    meta: dict[str, Any],
    rpm_block: str,
    *,
    extra: str = "",
) -> str:
    lines = ["**Prometheus / metrics**", ""]
    if rpm_block:
        lines.append(rpm_block)
        lines.append("")
    ctx = _format_alert_metric_context(meta)
    if ctx:
        lines.append(ctx)
        lines.append("")
    if extra:
        lines.append(extra)
    elif _wants_live_query(user, meta):
        lines.append(
            "_Live PromQL not configured._ Set PROMETHEUS_URL + PROMETHEUS_TOKEN (FWSS) "
            "for Haystack **fw-noc**._"
        )
    dashboard = meta.get("dashboard") or meta.get("dashboard1")
    if dashboard and "rpm" in user.lower():
        lines.append(f"<{dashboard}|Open Haystack dashboard>")
    return "\n".join(lines)


def _verbose_answer(
    user: str,
    meta: dict[str, Any],
    rpm_block: str,
    explicit: Optional[str],
    plan: Any,
    query_results: list[tuple[str, dict[str, Any], str]],
    metrics: MetricsBackend,
) -> str:
    lines = ["**Prometheus / metrics**", ""]
    if rpm_block:
        lines.append(rpm_block)
        lines.append("")
    if not explicit:
        ctx = _format_alert_metric_context(meta)
        if ctx:
            lines.append(ctx)
            lines.append("")

    if not query_results:
        lines.append("_No live series returned._")
        if plan.note:
            lines.append(f"_{plan.note}_")
        hint = PromClient._empty_query_hint(explicit or "")
        if hint:
            lines.append(hint)
        return "\n".join(lines)

    for promql, payload, source in query_results:
        header = f"**PromQL:** `{promql}`" if explicit else f"**Live metrics** (`{promql}`)"
        lines.append(f"{header} — {source}\n")
        lines.append(
            metrics.format_result(
                payload,
                source=source,
                promql=promql,
                summarize=not explicit and _is_natural_metrics_request(user),
            )
        )
        lines.append("")
    if plan.note:
        lines.append(f"_{plan.note}_")
    return "\n".join(lines).strip()


def _user_question(query: str) -> str:
    marker = "\n\nKubernetes context:"
    if marker in query:
        return query.split(marker, 1)[0].strip()
    return query.strip()


# Pod named explicitly in the question (quoted, `pod <name>`, or replicaset-style).
_QUOTED_NAME_RE = re.compile(r"""["'`]([A-Za-z0-9][A-Za-z0-9._-]{2,})["'`]""")
_NAMED_OBJECT_RE = re.compile(
    r"\b(?:pod|deployment|workload|container)\s+(?:named\s+|called\s+)?"
    r"[\"'`]?([A-Za-z0-9][A-Za-z0-9._-]*-[A-Za-z0-9]{4,})[\"'`]?",
    re.I,
)
_REPLICASET_POD_RE = re.compile(r"\b([a-z][a-z0-9-]+-[a-f0-9]{6,10}(?:-[a-z0-9]{5})?)\b")


def _pod_in_question(text: str) -> Optional[str]:
    """Extract a pod/workload name the user explicitly named in the question."""
    if not text:
        return None
    m = _QUOTED_NAME_RE.search(text)
    if m and "-" in m.group(1):
        return m.group(1)
    m = _NAMED_OBJECT_RE.search(text)
    if m:
        return m.group(1)
    m = _REPLICASET_POD_RE.search(text)
    if m:
        return m.group(1)
    return None


def _apply_question_overrides(user: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Prioritize a pod named in the question over stale alert metadata.

    When the user asks about a specific pod that differs from the alert's pod,
    drop the alert's RPM/threshold/summary so we answer the actual question
    instead of replaying the original alert.
    """
    named_pod = _pod_in_question(user)
    if not named_pod:
        return meta
    meta_pod = (meta.get("pod") or meta.get("alert_sre_attributes") or "").strip()
    if named_pod.lower() == meta_pod.lower():
        return meta

    new_meta = dict(meta)
    new_meta["pod"] = named_pod
    new_meta["alert_sre_attributes"] = named_pod
    for key in ("current_value", "threshold", "summary", "alertname", "dashboard", "dashboard1", "hostname"):
        new_meta.pop(key, None)
    fields = dict(new_meta.get("fields") or {})
    for key in ("current_value", "threshold", "summary", "hostname"):
        fields.pop(key, None)
    if fields:
        new_meta["fields"] = fields
    else:
        new_meta.pop("fields", None)
    logger.info("Question names pod %r; overriding alert pod %r", named_pod, meta_pod)
    return new_meta


def _parse_number(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _format_rpm_vs_threshold(meta: dict[str, Any], user: str) -> str:
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
        if is_haystack_tenant(hostname):
            lines.append(f"- **Haystack tenant:** `{hostname}`")
        else:
            lines.append(f"- **Host:** `{hostname}`")
    if summary:
        lines.append(f"- **Summary:** {summary}")
    return "\n".join(lines)


def _extract_promql(text: str) -> str | None:
    match = re.search(
        r"(?:promql|query)\s*[:=]\s*(.+?)(?:\s*$|\s*```)",
        text,
        re.I | re.DOTALL,
    )
    if match:
        q = match.group(1).strip().strip("`").strip("'").strip('"')
        return q or None
    if text.strip().startswith("{") or text.strip().startswith("sum("):
        return text.strip()
    return None


def _is_natural_metrics_request(text: str) -> bool:
    if _extract_promql(text):
        return False
    return bool(
        re.search(
            r"\b(fetch|get|show|what|current)\b.*\bmetrics?\b|\bmetrics?\b.*\b(alert|now|current)\b",
            text,
            re.I,
        )
    )


def _wants_live_query(text: str, meta: dict[str, Any]) -> bool:
    if re.search(
        r"\b(rpm|metric|metrics|prometheus|promql|current.?value|threshold|latency|"
        r"cpu|memory|haystack|dashboard|spike|rate|fetch|current)\b",
        text,
        re.I,
    ):
        return True
    if re.search(
        r"\b(fetch|get|show|what|current)\b.*\bmetrics?\b|\bmetrics?\b.*\b(now|current)\b",
        text,
        re.I,
    ):
        return True
    return bool(meta.get("current_value") or meta.get("threshold"))


def _format_alert_metric_context(meta: dict[str, Any]) -> str:
    parts: list[str] = []
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
    if not parts:
        return ""
    return "**Alert details**\n" + "\n".join(parts)


def main() -> None:
    host = os.environ.get("A2A_HOST", "0.0.0.0")
    port = int(os.environ.get("A2A_PORT", str(DEFAULT_PORT)))
    PrometheusAgent(host=host, port=port).run()


if __name__ == "__main__":
    main()
