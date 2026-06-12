"""
LLM-driven agent loop for the Freshservice NOC agent.

Two execution paths (same pattern as kubernetes/agent_loop.py):
1. Native OpenAI tool-calling — preferred.
2. JSON-planner fallback for gateways without tool support.

All tools are read-only. The loop gathers evidence via Freshservice/Freshstatus/MySQL
tools, then calls submit_report once to deliver a structured Slack answer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from agents.freshservice.mcp_server import (
    FreshserviceToolDispatcher,
    TOOL_SPECS,
    ToolResult,
)
from agents.freshservice.prompt_builder import build_system_prompt, build_user_payload
from agents.freshservice.report import SUBMIT_REPORT_TOOL, render_report
from common.llm import LLMClient, ToolsUnsupported

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 8
SUBMIT_REPORT_NAME = "submit_report"


@dataclass
class AgentResult:
    answer: str
    route: str = "llm-mcp"
    steps: int = 0


def run_agent_loop(
    query: str,
    *,
    metadata: dict[str, Any] | None = None,
    thread_messages: list[dict] | None = None,
    dispatcher: FreshserviceToolDispatcher,
    llm: Optional[LLMClient] = None,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> AgentResult:
    """Run the LLM tool-calling loop and return a synthesized answer."""
    llm = llm or LLMClient()
    specs = TOOL_SPECS + [SUBMIT_REPORT_TOOL]

    system_prompt = build_system_prompt()
    user_content = query

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    try:
        return _native_tool_loop(llm, dispatcher, specs, messages, max_steps)
    except ToolsUnsupported:
        logger.info("Gateway lacks tool-calling; using JSON-planner fallback")
        return _json_planner_loop(llm, dispatcher, specs, query, max_steps)


def _native_tool_loop(
    llm: LLMClient,
    dispatcher: FreshserviceToolDispatcher,
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
            forced = _force_submit_report(llm, specs, messages, content)
            if forced is not None:
                return AgentResult(answer=forced, route="llm-mcp", steps=step)
            return AgentResult(
                answer=last_text or "_No answer produced._", route="llm-mcp", steps=step
            )

        messages.append(message)
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments") or "{}"
            args = _safe_json(raw_args)

            if name == SUBMIT_REPORT_NAME:
                return AgentResult(answer=render_report(args), route="llm-mcp", steps=step)

            result = dispatcher.call_tool(name, args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": _result_to_text(result),
                }
            )

    forced = _force_submit_report(llm, specs, messages, last_text)
    if forced is not None:
        return AgentResult(answer=forced, route="llm-mcp", steps=max_steps)
    return AgentResult(
        answer=last_text or "_No answer produced._", route="llm-mcp", steps=max_steps
    )


def _force_submit_report(
    llm: LLMClient,
    specs: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    content: str,
) -> Optional[str]:
    msgs = list(messages)
    if content:
        msgs.append({"role": "assistant", "content": content})
    msgs.append(
        {
            "role": "user",
            "content": (
                "Deliver the final answer now using the submit_report tool call. "
                "Fill in headline, severity, summary, key_facts, root_cause, next_steps, links. "
                "Do not write prose or JSON in the message body."
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
        logger.info("Forced submit_report unavailable (%s); using prose", exc)
        return None
    for call in message.get("tool_calls") or []:
        if (call.get("function") or {}).get("name") == SUBMIT_REPORT_NAME:
            args = _safe_json((call.get("function") or {}).get("arguments") or "{}")
            return render_report(args)
    return None


def _json_planner_loop(
    llm: LLMClient,
    dispatcher: FreshserviceToolDispatcher,
    specs: list[dict[str, Any]],
    query: str,
    max_steps: int,
) -> AgentResult:
    tool_catalog = "\n".join(
        f"- {s['function']['name']}: {s['function']['description']}" for s in specs
    )
    planner_system = (
        build_system_prompt()
        + "\n\nYou do NOT have native tool calling. Each turn respond with ONLY a JSON object:\n"
        '{"tool": <tool name>, "args": {<arguments>}}\n'
        "Call tools to gather data. When done, set tool=\"submit_report\" and put the "
        "structured report fields (headline, severity, summary, key_facts, root_cause, "
        "next_steps, links) in args.\n\n"
        f"Available tools:\n{tool_catalog}"
    )
    observations: list[str] = []
    for step in range(1, max_steps + 1):
        obs_block = ""
        if observations:
            obs_block = "\n\nObservations so far:\n" + "\n".join(observations)
        user = f"User question: {query}{obs_block}"
        try:
            plan = llm.complete_json(system=planner_system, user=user, max_tokens=1200)
        except Exception as exc:
            logger.warning("Planner JSON call failed: %s", exc)
            break
        tool = plan.get("tool")
        args = plan.get("args") or {}
        if tool == SUBMIT_REPORT_NAME:
            return AgentResult(answer=render_report(args), route="llm-mcp-json", steps=step)
        if not tool:
            answer = (plan.get("answer") or "").strip()
            if answer:
                return AgentResult(answer=answer, route="llm-mcp-json", steps=step)
            break
        result = dispatcher.call_tool(tool, args)
        observations.append(f"{tool} -> {_result_to_text(result, limit=3000)}")

    # Final forced pass
    summary_user = (
        f"User question: {query}\n\nObservations:\n"
        + "\n".join(observations)
        + '\n\nRespond now with {"tool": "submit_report", "args": {<report fields>}}.'
    )
    try:
        final = llm.complete_json(system=planner_system, user=summary_user, max_tokens=1200)
        if final.get("tool") == SUBMIT_REPORT_NAME:
            return AgentResult(answer=render_report(final.get("args") or {}), route="llm-mcp-json", steps=max_steps)
    except Exception as exc:
        logger.warning("Final forced submit_report failed: %s", exc)

    return AgentResult(
        answer="\n".join(observations) or "_No data retrieved._",
        route="llm-mcp-json",
        steps=max_steps,
    )


def _result_to_text(result: ToolResult, limit: int = 4000) -> str:
    data = result.data
    if isinstance(data, str):
        return data[:limit]
    try:
        text = json.dumps(data, default=str)
    except Exception:
        text = str(data)
    return text[:limit]


def _safe_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text) or {}
    except Exception:
        return {}
