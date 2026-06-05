"""PromQL planning — LLM + fw-noc rules, with validation and fallbacks."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from common.haystack_tenant import (
    is_freshdesk_app_id,
    is_haystack_tenant,
    resolve_haystack_tenant,
)
from agents.prometheus.prom_client import PromClient, build_live_promql
from common.llm import LLMClient

logger = logging.getLogger(__name__)

_PROMQL_SAFE = re.compile(r"""^[a-zA-Z0-9_:,\{\}\.\s\-\+\*\/\(\)\[\]|=!~?^$"']+$""")
_MAX_PROMQL_LEN = 500


@dataclass
class PromqlPlan:
    queries: list[str] = field(default_factory=list)
    source: str = "rules"
    note: str = ""


def _system_prompt() -> str:
    return (
        "You are a Prometheus expert for Haystack tenant fw-noc (Freshworks NOC).\n"
        "Return JSON only:\n"
        '{"queries":["<promql>", ...], "note": "<one line why>"}\n'
        "Rules:\n"
        "- Use 1-3 instant PromQL queries (not range).\n"
        "- Metric `up` has label k8s_cluster_name but NOT namespace — never use namespace= on up{}.\n"
        "- hostname may be Haystack tenant (e.g. fw-noc) — use X-Scope-OrgID, NOT {hostname=\"fw-noc\"}.\n"
        "- hostname=app-<digits> is a Freshdesk app id, NOT a Prom label — use pod/namespace/k8s_cluster_name.\n"
        "- For pod crash/RPM Kube alerts prefer kube_pod_container_status_waiting_reason, "
        "kube_pod_container_status_restarts_total, kube_pod_status_ready.\n"
        "- CPU usage (cores): sum(rate(container_cpu_usage_seconds_total{...,container!=\"\"}[5m])) "
        "by (pod). Memory: sum(container_memory_working_set_bytes{...,container!=\"\"}) by (pod). "
        "Always add container!=\"\" to exclude the pod-level cgroup roll-up.\n"
        "- Answer the user's QUESTION metric: if they ask CPU/memory of a named pod, query that "
        "metric for that pod — do NOT substitute kube_pod_* health metrics.\n"
        "- Always include k8s_cluster_name when cluster is known.\n"
        "- Labels: pod, namespace, k8s_cluster_name, container, reason, condition.\n"
    )


def _validate_promql(promql: str) -> Optional[str]:
    q = (promql or "").strip()
    if not q or len(q) > _MAX_PROMQL_LEN:
        return None
    if not _PROMQL_SAFE.match(q):
        return None
    if ".." in q or ";;" in q:
        return None
    return q


def _label_selector(meta: dict[str, Any]) -> str:
    pod = (meta.get("pod") or meta.get("alert_sre_attributes") or "").strip()
    namespace = (meta.get("namespace") or "").strip()
    cluster = (meta.get("cluster") or "").strip()
    parts: list[str] = []
    if pod and not is_freshdesk_app_id(pod) and not is_haystack_tenant(pod):
        parts.append(f'pod="{pod}"')
    if namespace:
        parts.append(f'namespace="{namespace}"')
    if cluster:
        parts.append(f'k8s_cluster_name="{cluster}"')
    return ",".join(parts)


def build_focused_queries(meta: dict[str, Any]) -> list[str]:
    """Small set of high-signal queries for Slack pod/RPM alerts."""
    sel = _label_selector(meta)
    if not sel:
        cluster = (meta.get("cluster") or "").strip()
        if cluster:
            return [f'up{{k8s_cluster_name="{cluster}"}}']
        return []

    return [
        f"kube_pod_container_status_waiting_reason{{{sel}}}",
        f"kube_pod_container_status_restarts_total{{{sel}}}",
        f'kube_pod_status_ready{{{sel},condition="false"}}',
    ]


def plan_promql(
    user_question: str,
    meta: dict[str, Any],
    *,
    llm: Optional[LLMClient] = None,
    prom: Optional[PromClient] = None,
    explicit: Optional[str] = None,
) -> PromqlPlan:
    if explicit:
        q = _validate_promql(explicit)
        if q:
            return PromqlPlan(queries=[q], source="user", note="Explicit PromQL from message")
        return PromqlPlan(queries=[], source="user", note="Invalid PromQL in message")

    use_llm = os.environ.get("PROMETHEUS_LLM_PROMQL", "true").lower() not in (
        "0",
        "false",
        "no",
    )
    client = llm or LLMClient()
    if use_llm and client.enabled():
        plan = _plan_with_llm(user_question, meta, client)
        if plan.queries:
            plan = _probe_and_refine(plan, meta, prom)
            if plan.queries:
                return plan

    template = build_live_promql(meta)
    if template and "kube_pod_" not in template:
        focused = build_focused_queries(meta)
        if focused:
            return PromqlPlan(
                queries=focused,
                source="rules",
                note="Focused pod health metrics (kube-state)",
            )
        return PromqlPlan(queries=[template], source="rules", note="Label selector from alert")

    focused = build_focused_queries(meta)
    if focused:
        return PromqlPlan(
            queries=focused,
            source="rules",
            note="Focused pod health metrics (kube-state)",
        )
    if template:
        return PromqlPlan(queries=[template], source="rules", note="Label selector from alert")
    return PromqlPlan(queries=[], source="rules", note="No PromQL could be built from alert context")


def _plan_with_llm(
    user_question: str,
    meta: dict[str, Any],
    llm: LLMClient,
) -> PromqlPlan:
    context = (
        f"Question: {user_question}\n"
        f"alertname={meta.get('alertname')}\n"
        f"product={meta.get('product')}\n"
        f"hostname={meta.get('hostname')}\n"
        f"haystack_tenant={resolve_haystack_tenant(meta)}\n"
        f"pod={meta.get('pod') or meta.get('alert_sre_attributes')}\n"
        f"namespace={meta.get('namespace')}\n"
        f"cluster={meta.get('cluster')}\n"
        f"current_value={meta.get('current_value')}\n"
        f"threshold={meta.get('threshold')}\n"
        f"summary={meta.get('summary')}\n"
    )
    try:
        raw = llm.complete_json(system=_system_prompt(), user=context, max_tokens=400)
        queries_raw = raw.get("queries") or []
        if not queries_raw and raw.get("promql"):
            queries_raw = [raw["promql"]]
        validated = []
        for q in queries_raw[:3]:
            v = _validate_promql(str(q))
            if v:
                validated.append(v)
        note = str(raw.get("note") or "LLM-selected PromQL")
        if validated:
            logger.info("LLM PromQL plan: %s", validated)
            return PromqlPlan(queries=validated, source=f"llm/{llm.model}", note=note)
    except Exception as exc:
        logger.warning("LLM PromQL planning failed: %s", exc)
    return PromqlPlan(queries=[], source="llm-fallback", note="")


def _probe_and_refine(
    plan: PromqlPlan,
    meta: dict[str, Any],
    prom: Optional[PromClient],
) -> PromqlPlan:
    if not prom or not prom.live_query_enabled:
        return plan
    first = plan.queries[0]
    try:
        org = resolve_haystack_tenant(meta)
        payload = prom.query(first, org_id=org)
        n = len((payload.get("data") or {}).get("result") or [])
        if n > 0:
            return plan
        logger.info("LLM/query probe returned 0 series for %s", first)
    except Exception as exc:
        logger.warning("PromQL probe failed: %s", exc)

    # Only fall back to pod-health metrics if the failing query was itself a
    # health/up query. NEVER replace a CPU/memory/custom metric with kube_pod_*
    # — that silently answers a different question than the user asked.
    is_health_like = "kube_pod_" in first or first.strip().startswith("up")
    if not is_health_like:
        return PromqlPlan(
            queries=plan.queries,
            source=plan.source,
            note="No series returned — the pod may not exist (check the current pod name) "
            "or is not emitting this metric.",
        )

    # Bad label combo (e.g. up{namespace=}) — fall back to focused pod queries
    if _label_selector(meta):
        focused = build_focused_queries(meta)
        if focused:
            return PromqlPlan(
                queries=focused,
                source="rules",
                note="Adjusted: LLM query returned no series; using pod health metrics",
            )
    cluster = (meta.get("cluster") or "").strip()
    if cluster:
        return PromqlPlan(
            queries=[f'up{{k8s_cluster_name="{cluster}"}}'],
            source="rules",
            note="Adjusted: cluster up{} (no namespace label on up in fw-noc)",
        )
    return plan
