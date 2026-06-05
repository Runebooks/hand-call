"""Compact Slack-friendly formatting for prometheus-agent replies."""

from __future__ import annotations

import re
from typing import Any, Optional

from common.haystack_tenant import (
    is_freshdesk_app_id,
    is_haystack_tenant,
    resolve_haystack_tenant,
)
from agents.prometheus.prom_client import PromClient
from agents.prometheus.promql_planner import PromqlPlan


def compact_slack_enabled() -> bool:
    import os

    return os.environ.get("PROMETHEUS_COMPACT_SLACK", "true").lower() not in (
        "0",
        "false",
        "no",
    )


def format_compact_response(
    *,
    user_question: str,
    meta: dict[str, Any],
    plan: PromqlPlan,
    query_results: list[tuple[str, dict[str, Any], str]],
    rpm_block: str,
    explicit_promql: bool,
) -> str:
    lines: list[str] = []
    pod = (meta.get("pod") or meta.get("alert_sre_attributes") or "").strip()
    namespace = (meta.get("namespace") or "").strip()
    host = (meta.get("hostname") or "").strip()
    tenant = resolve_haystack_tenant(meta)

    snapshot = _build_snapshot(query_results)
    summary = _one_line_summary(meta, snapshot, rpm_block)
    if summary:
        lines.append(f"**Summary:** {summary}")
        lines.append("")

    if snapshot and pod:
        lines.append(f"**Pod snapshot** (`{namespace}/{pod}`)" if namespace else f"**Pod snapshot** (`{pod}`)")
        lines.extend(_snapshot_bullets(snapshot))
        lines.append("")

    if rpm_block:
        lines.append("**RPM (Trigmetry alert)**")
        lines.extend(_rpm_compact_lines(rpm_block))
        lines.append("")

    if not explicit_promql and not compact_slack_enabled():
        pass
    elif query_results:
        lines.append("**Live Haystack**")
        for promql, payload, source in query_results:
            n = len((payload.get("data") or {}).get("result") or [])
            if len(query_results) == 1:
                lines.append(f"- Query: `{promql}` → {n} series ({source})")
            else:
                lines.append(f"- `{_short_query_name(promql)}` → {n} series")
            if not _short_query_name(promql).startswith("kube_pod_"):
                for value_line in _value_lines(promql, payload):
                    lines.append(f"  ↳ {value_line}")
        if plan.note:
            lines.append(f"_{plan.note}_")
        if plan.source.startswith("llm"):
            lines.append(f"_PromQL via {plan.source}_")
        lines.append(f"_Tenant: **{tenant}** (`X-Scope-OrgID`)_")
        lines.append("")

    if is_haystack_tenant(host) and not explicit_promql:
        lines.append(f"_Haystack tenant **{tenant}** (from alert `hostname`)._")
    elif host.startswith("app-") and not explicit_promql:
        lines.append(
            f"_`{host}` is a Freshdesk app id; live pod metrics use "
            f"`{pod}` in tenant **{tenant}**._"
        )

    if explicit_promql and query_results:
        promql, payload, source = query_results[0]
        detail = PromClient.format_instant_result(
            payload, promql=promql, summarize=False, limit=6
        )
        lines.append(detail)

    return "\n".join(lines).strip()


def _one_line_summary(
    meta: dict[str, Any],
    snapshot: dict[str, Any],
    rpm_block: str,
) -> str:
    parts: list[str] = []
    m = re.search(r"\*\*Current value:\*\* `([^`]+)`", rpm_block)
    t = re.search(r"\*\*Threshold:\*\* `([^`]+)`", rpm_block)
    if m and t:
        try:
            cur, thr = float(m.group(1)), float(t.group(1))
            if cur > thr:
                parts.append(f"RPM {cur:g} > {thr:g}")
            elif cur < thr:
                parts.append(f"RPM {cur:g} < {thr:g}")
            else:
                parts.append(f"RPM {cur:g} at threshold")
        except ValueError:
            pass

    host = (meta.get("hostname") or "").strip()
    if host and is_haystack_tenant(host):
        parts.append(f"tenant {host}")
    elif host and is_freshdesk_app_id(host):
        parts.append(host)

    pod = (meta.get("pod") or meta.get("alert_sre_attributes") or "").strip()
    if snapshot.get("waiting_reason"):
        parts.append(f"{pod or 'pod'} {snapshot['waiting_reason']}")
    elif snapshot.get("phase"):
        parts.append(f"{pod or 'pod'} phase={snapshot['phase']}")

    if snapshot.get("restarts") is not None:
        parts.append(f"{snapshot['restarts']:g} restarts")

    return " · ".join(parts)


def _build_snapshot(
    query_results: list[tuple[str, dict[str, Any], str]],
) -> dict[str, Any]:
    snap: dict[str, Any] = {}
    for _promql, payload, _source in query_results:
        for item in (payload.get("data") or {}).get("result") or []:
            metric = item.get("metric") or {}
            name = metric.get("__name__", "")
            try:
                val = float(item.get("value", [None, 0])[1])
            except (TypeError, ValueError):
                val = 0.0

            if name == "kube_pod_container_status_waiting_reason" and val >= 1:
                snap["waiting_reason"] = metric.get("reason") or "Waiting"
            if name == "kube_pod_container_status_restarts_total":
                snap["restarts"] = max(snap.get("restarts") or 0, val)
            if name == "kube_pod_status_ready" and metric.get("condition") == "false":
                snap["ready_false"] = val
            if name == "kube_pod_status_ready" and metric.get("condition") == "true":
                snap["ready_true"] = val
            if name == "kube_pod_status_phase" and val >= 1:
                snap["phase"] = metric.get("phase") or ""
    return snap


def _snapshot_bullets(snapshot: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if snapshot.get("waiting_reason"):
        lines.append(f"· **State:** {snapshot['waiting_reason']}")
    if snapshot.get("restarts") is not None:
        lines.append(f"· **Restarts:** {snapshot['restarts']:g}")
    if snapshot.get("ready_false") is not None:
        ready = "not ready" if snapshot.get("ready_false", 0) >= 1 else "ready"
        lines.append(f"· **Ready:** {ready}")
    if snapshot.get("phase"):
        lines.append(f"· **Phase:** {snapshot['phase']}")
    if not lines:
        lines.append("· _No pod snapshot lines extracted — see live query below._")
    return lines


def _rpm_compact_lines(rpm_block: str) -> list[str]:
    lines = []
    for pat, label in (
        (r"\*\*Current value:\*\* `([^`]+)`", "Current"),
        (r"\*\*Threshold:\*\* `([^`]+)`", "Threshold"),
        (r"\*\*Comparison:\*\* (.+)$", "Status"),
    ):
        m = re.search(pat, rpm_block, re.M)
        if m:
            lines.append(f"· **{label}:** {m.group(1).strip()}")
    return lines or [rpm_block]


def _short_query_name(promql: str) -> str:
    m = re.match(r"([a-zA-Z_:][a-zA-Z0-9_:]*)", promql)
    return m.group(1) if m else promql[:40]


def _humanize_value(promql: str, val: float) -> str:
    p = promql.lower()
    if "_bytes" in p or "memory_working_set" in p or "memory_usage" in p:
        for unit, div in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
            if abs(val) >= div:
                return f"{val / div:.2f} {unit}"
        return f"{val:.0f} B"
    if "cpu_usage_seconds" in p or "cpu_seconds" in p or "cpu_cores" in p:
        return f"{val:.4f} cores (~{val * 1000:.1f} millicores)"
    if val == int(val):
        return f"{val:g}"
    return f"{val:.4g}"


def _value_lines(promql: str, payload: dict[str, Any], limit: int = 4) -> list[str]:
    """Render the actual scalar value(s) of an instant query result."""
    results = (payload.get("data") or {}).get("result") or []
    lines: list[str] = []
    for item in results[:limit]:
        try:
            val = float(item.get("value", [None, None])[1])
        except (TypeError, ValueError, IndexError):
            continue
        metric = item.get("metric") or {}
        label = metric.get("pod") or metric.get("instance") or metric.get("container") or ""
        human = _humanize_value(promql, val)
        lines.append(f"**{human}**" + (f" — `{label}`" if label and len(results) > 1 else ""))
    if len(results) > limit:
        lines.append(f"…and {len(results) - limit} more series")
    return lines
