"""
LLM-driven agent loop (the "brain") for the Kubernetes agent.

The LLM interprets the user's intent and calls MCP tools (see ``mcp_server.py``)
to fetch real cluster data, then writes an accurate answer. Two execution paths:

1. Native OpenAI tool-calling (``llm.chat(tools=...)``) — preferred.
2. JSON-planner fallback when the gateway does not support tools — the LLM
   returns ``{"tool": ..., "args": ..., "answer": ...}`` each turn.

Mutations never execute here: a mutation tool returns ``confirmation_required``
and a ``PendingMutation``, which the loop surfaces so ``server.py`` can run the
Slack yes/no confirmation flow.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from agents.kubernetes.kube_client import (
    KubeClient,
    parse_deployment_name,
)
from agents.kubernetes.mcp_server import (
    KubeToolDispatcher,
    ToolResult,
    tool_specs,
)
from agents.kubernetes.mutations import mutations_enabled
from agents.kubernetes.pending import PendingMutation
from agents.kubernetes.report import SUBMIT_REPORT_TOOL, render_report
from common.llm import LLMClient, ToolsUnsupported

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 5
SUBMIT_REPORT_NAME = "submit_report"


@dataclass
class AgentResult:
    answer: str
    pending: Optional[PendingMutation] = None
    route: str = "llm-mcp"
    steps: int = 0


def _system_prompt() -> str:
    mutate = mutations_enabled()
    scope = (
        "You can investigate (read-only) AND request staging mutations "
        "(restart/delete/scale/rollout) which require user yes/no confirmation."
        if mutate
        else "You are READ-ONLY: investigate and explain only. Never attempt mutations."
    )
    return f"""You are a Kubernetes NOC on-call assistant. {scope}

You answer by calling tools to fetch REAL cluster data, then summarizing.
Rules:
- ALWAYS call tools to get facts before answering; never invent pod names, statuses, logs, or replica counts.
- 'why is the pod restarting/crashing' -> use problem_pods and/or pod_logs (previous=true) and find_pod.
- 'how many replicas' / 'replica count' -> use pod_workload or deployments (this is READ-ONLY; do NOT scale).
- 'which deployment owns this pod' / 'deployment name' -> use pod_workload.
- 'how many pods in namespace X' -> use namespace_pod_summary or list_pods.
- 'get logs' -> use pod_logs.
- Strip ReplicaSet suffix: pod test-agent-workload-5675697b79-2xh2z belongs to deployment test-agent-workload.
- Never treat the namespace value as a pod name.
- TARGET PRIORITY: if the user's question explicitly names a pod or deployment, that named workload is the
  target. The Slack/NOC metadata namespace/pod is only a fallback (it comes from the originating alert and
  may be stale) — use it only when the question does not name its own workload. Never answer about the
  metadata pod when the question clearly asks about a different workload.
- When the user asks to change the cluster (restart/delete/scale/rollout), you MUST call the matching
  mutation tool (restart_pod/delete_pod/scale_deployment/rollout_restart). Do NOT ask for confirmation in
  your own words and do NOT gather extra data first — calling the tool is what triggers the system's
  yes/no confirmation, and the change will NOT run until the user confirms. You may call a read tool only
  if you genuinely cannot identify the target pod/deployment.

OUTPUT: You MUST deliver the final answer by CALLING the `submit_report` tool. Never write the report as
plain text, YAML, or JSON in your message content — always use the tool call. Highlight only the key data
points as facts; keep the summary to 1-2 sentences. If logs matter, put the few most relevant lines in
log_excerpt and any long output in full_logs."""


def _context_user_message(query: str, namespace: Optional[str], pod: Optional[str]) -> str:
    meta = []
    if namespace:
        meta.append(f"namespace={namespace}")
    if pod:
        meta.append(f"pod={pod}")
    meta_str = (
        "\nFallback target from originating alert (use only if the question names no workload): "
        + ", ".join(meta)
        if meta
        else ""
    )
    return f"User question: {query}{meta_str}"


def _user_question(query: str) -> str:
    """Strip the appended cluster-context block to get the on-call's words."""
    for marker in ("\n\nKubernetes context:", "\n\nUse cluster context:"):
        if marker in query:
            return query.split(marker, 1)[0].strip()
    return query.strip()


def _named_workload_in_question(query: str, namespace: Optional[str]) -> Optional[str]:
    """A workload explicitly named by the on-call's words (not the alert).

    Conservative on purpose: matches `deployment/pod <name>` phrases or a
    DNS-style hyphenated token (e.g. `inventory-sync`), never plain English.
    """
    user = _user_question(query)
    explicit = parse_deployment_name(user)
    if explicit:
        return explicit
    m = re.search(r"\b(?:pod|deployment|deploy)\s+[`'\"]?([a-z0-9][-a-z0-9]*)", user, re.I)
    if m:
        return m.group(1).lower()
    ns = (namespace or "").lower()
    for token in re.findall(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+", user.lower()):
        if token != ns:
            return token
    return None


def _effective_pod(query: str, namespace: Optional[str], pod: Optional[str]) -> Optional[str]:
    """Prefer a workload named in the question over a stale alert metadata pod.

    When the question explicitly names a pod/deployment that differs from the
    alert metadata pod, drop the metadata pod so the LLM targets the named
    workload (its name is in the query text) instead of the alert's pod.
    """
    named = _named_workload_in_question(query, namespace)
    if named and (not pod or named.lower() != pod.lower()):
        logger.info(
            "Question names workload %r; overriding alert metadata pod %r", named, pod
        )
        return None
    return pod


def run_agent_loop(
    query: str,
    *,
    namespace: Optional[str] = None,
    pod: Optional[str] = None,
    session_id: Optional[str] = None,
    kube: KubeClient,
    llm: Optional[LLMClient] = None,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> AgentResult:
    """Run the LLM tool-calling loop and return a synthesized answer."""
    llm = llm or LLMClient()
    dispatcher = KubeToolDispatcher(kube)
    pod = _effective_pod(query, namespace, pod)
    # submit_report is a terminal tool: the LLM calls it to deliver the answer.
    specs = tool_specs() + [SUBMIT_REPORT_TOOL]

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": _context_user_message(query, namespace, pod)},
    ]

    try:
        return _native_tool_loop(llm, dispatcher, specs, messages, max_steps)
    except ToolsUnsupported:
        logger.info("Gateway lacks tool-calling; using JSON-planner fallback")
        return _json_planner_loop(llm, dispatcher, specs, query, namespace, pod, max_steps)


def _native_tool_loop(
    llm: LLMClient,
    dispatcher: KubeToolDispatcher,
    specs: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    max_steps: int,
) -> AgentResult:
    last_text = ""
    for step in range(1, max_steps + 1):
        message = llm.chat(messages, tools=specs, tool_choice="auto")
        tool_calls = message.get("tool_calls") or []
        content = (message.get("content") or "").strip()
        if content:
            last_text = content
        if not tool_calls:
            # Model answered in prose instead of calling submit_report; force a
            # structured report so output stays consistent.
            forced = _force_submit_report(llm, specs, messages, content)
            if forced is not None:
                return AgentResult(answer=forced, route="llm-mcp", steps=step)
            return AgentResult(
                answer=last_text or "_No answer produced._", route="llm-mcp", steps=step
            )

        messages.append(message)
        for call in tool_calls:
            name = (call.get("function") or {}).get("name", "")
            raw_args = (call.get("function") or {}).get("arguments") or "{}"
            args = _safe_json(raw_args)
            if name == SUBMIT_REPORT_NAME:
                return AgentResult(answer=render_report(args), route="llm-mcp", steps=step)
            result = dispatcher.call_tool(name, args)
            if result.pending is not None:
                return AgentResult(
                    answer=last_text,
                    pending=result.pending,
                    route="llm-mcp",
                    steps=step,
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": _result_to_text(result),
                }
            )

    # Out of steps: force a structured final answer.
    forced = _force_submit_report(llm, specs, messages, last_text)
    if forced is not None:
        return AgentResult(answer=forced, route="llm-mcp", steps=max_steps)
    return AgentResult(
        answer=last_text or "_No answer produced._",
        route="llm-mcp",
        steps=max_steps,
    )


def _force_submit_report(
    llm: LLMClient,
    specs: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    content: str,
) -> Optional[str]:
    """Best-effort: make the model emit a structured submit_report tool call.

    Returns rendered markdown, or None if the gateway cannot honor a forced
    tool choice (caller then falls back to the prose content).
    """
    msgs = list(messages)
    if content:
        msgs.append({"role": "assistant", "content": content})
    msgs.append(
        {
            "role": "user",
            "content": (
                "Deliver that as a submit_report tool call now with structured fields. "
                "Do not write prose, YAML, or JSON in the message."
            ),
        }
    )
    try:
        message = llm.chat(
            msgs,
            tools=specs,
            tool_choice={"type": "function", "function": {"name": SUBMIT_REPORT_NAME}},
        )
    except Exception as exc:
        logger.info("Forced submit_report not available (%s); using prose", exc)
        return None
    for call in message.get("tool_calls") or []:
        if (call.get("function") or {}).get("name") == SUBMIT_REPORT_NAME:
            args = _safe_json((call.get("function") or {}).get("arguments") or "{}")
            return render_report(args)
    return None


def _json_planner_loop(
    llm: LLMClient,
    dispatcher: KubeToolDispatcher,
    specs: list[dict[str, Any]],
    query: str,
    namespace: Optional[str],
    pod: Optional[str],
    max_steps: int,
) -> AgentResult:
    tool_catalog = "\n".join(
        f"- {s['function']['name']}: {s['function']['description']}" for s in specs
    )
    planner_system = (
        _system_prompt()
        + "\n\nYou do NOT have native tool calling. Each turn respond with ONLY a JSON object:\n"
        '{"tool": <tool name>, "args": {<arguments>}}\n'
        "Call read/mutation tools to gather data. When done, set tool=\"submit_report\" and put the "
        "structured report fields (headline, severity, summary, facts, root_cause, log_excerpt, "
        "full_logs, logs_label, next_steps, can_do) in args.\n\n"
        f"Available tools:\n{tool_catalog}"
    )
    observations: list[str] = []
    for step in range(1, max_steps + 1):
        obs_block = ""
        if observations:
            obs_block = "\n\nObservations so far:\n" + "\n".join(observations)
        user = _context_user_message(query, namespace, pod) + obs_block
        try:
            plan = llm.complete_json(system=planner_system, user=user, max_tokens=1024)
        except Exception as exc:
            logger.warning("Planner JSON call failed: %s", exc)
            break
        tool = plan.get("tool")
        args = plan.get("args") or {}
        if tool == SUBMIT_REPORT_NAME:
            return AgentResult(answer=render_report(args), route="llm-mcp-json", steps=step)
        # Backward-compatible: a free-form answer with no tool also terminates.
        if not tool:
            answer = (plan.get("answer") or "").strip()
            if answer:
                return AgentResult(answer=answer, route="llm-mcp-json", steps=step)
            break
        result = dispatcher.call_tool(tool, args)
        if result.pending is not None:
            return AgentResult(answer="", pending=result.pending, route="llm-mcp-json", steps=step)
        observations.append(f"{tool} -> {_result_to_text(result, limit=2500)}")

    # Final pass: force a structured submit_report.
    summary_user = (
        _context_user_message(query, namespace, pod)
        + "\n\nObservations:\n"
        + "\n".join(observations)
        + '\n\nRespond now with {"tool": "submit_report", "args": {<report fields>}}.'
    )
    try:
        final = llm.complete_json(system=planner_system, user=summary_user, max_tokens=1024)
        if final.get("tool") == SUBMIT_REPORT_NAME:
            return AgentResult(
                answer=render_report(final.get("args") or {}),
                route="llm-mcp-json",
                steps=max_steps,
            )
        ans = (final.get("answer") or "").strip()
        if ans:
            return AgentResult(answer=ans, route="llm-mcp-json", steps=max_steps)
    except Exception as exc:
        logger.warning("Planner summary failed: %s", exc)
    return AgentResult(answer="_No answer produced._", route="llm-mcp-json", steps=max_steps)


def _safe_json(raw: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _result_to_text(result: ToolResult, limit: int = 6000) -> str:
    try:
        text = json.dumps(result.data, default=str)
    except (TypeError, ValueError):
        text = str(result.data)
    if len(text) > limit:
        text = text[:limit] + "...(truncated)"
    return text
