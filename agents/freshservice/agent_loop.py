"""
LLM-driven agent loop for the Freshservice NOC agent.

Two execution paths (same pattern as kubernetes/agent_loop.py):
1. Native OpenAI tool-calling — preferred.
2. JSON-planner fallback for gateways without tool support.

PIR enforcement strategy (strict gate + targeted retry):
- Intent classifier flags: who / timeline / detail / metrics.
- For detail-level intents on a specific ticket, get_pir MUST be called before
  submit_report is allowed.  If the model tries to skip it, a corrective turn
  re-forces the call.
- After submit_report, if intent=who and personnel is empty but the captured PIR
  narrative has names, a one-shot retry re-prompts the LLM to extract them.
  Same rule for intent=timeline with an empty timeline list.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from agents.freshservice.mcp_server import (
    FreshserviceToolDispatcher,
    enabled_tool_specs,
    ToolResult,
)
from agents.freshservice.prompt_builder import build_system_prompt, build_user_payload
from agents.freshservice.report import SUBMIT_REPORT_TOOL, render_report
from common.llm import LLMClient, ToolsUnsupported

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 10
SUBMIT_REPORT_NAME = "submit_report"

# ---------------------------------------------------------------------------
# Intent classification helpers
# ---------------------------------------------------------------------------

_MI_ID_RE = re.compile(r"\bMI[-_]?(\d{5,8})\b", re.IGNORECASE)
_WHO_RE = re.compile(
    r"\b(who|involved|responder|on[-\s]?call|owner|assigned|joined|personnel|people|team|engineer|lead)\b",
    re.IGNORECASE,
)
_TIMELINE_RE = re.compile(
    r"\b(timeline|chronolog|what happened|sequence|when did|steps|walk.?me.?through|history)\b",
    re.IGNORECASE,
)
_METRICS_RE = re.compile(r"\b(mttr|mttd|mtta|duration|how long|time.?to)\b", re.IGNORECASE)
_DETAIL_RE = re.compile(
    r"\b(root.?cause|rca|impact|details|briefing|outage on|incident on|post.?incident|pir)\b",
    re.IGNORECASE,
)


def _classify_intent(query: str) -> set[str]:
    intents: set[str] = set()
    if _WHO_RE.search(query):
        intents.add("who")
    if _TIMELINE_RE.search(query):
        intents.add("timeline")
    if _METRICS_RE.search(query):
        intents.add("metrics")
    if _DETAIL_RE.search(query):
        intents.add("detail")
    if _MI_ID_RE.search(query):
        intents.add("detail")
    return intents


def _extract_mi_id(query: str) -> Optional[str]:
    m = _MI_ID_RE.search(query)
    return m.group(1) if m else None


def _normalize_thread(thread_messages: list[dict] | None, max_turns: int = 8) -> list[dict]:
    """Convert Slack thread messages into chat turns for context.

    Accepts a list of {"role": "user"/"assistant", "text"/"content": str} dicts
    (most recent allowed at any position) and returns the last `max_turns`
    normalized {"role", "content"} chat messages. Bot replies become assistant
    turns; user messages become user turns. Empty/oversized entries are skipped.
    """
    if not thread_messages:
        return []
    out: list[dict] = []
    for m in thread_messages:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            # Heuristic: treat bot/agent messages as assistant, else user
            role = "assistant" if m.get("is_bot") else "user"
        text = (m.get("content") or m.get("text") or "").strip()
        if not text:
            continue
        out.append({"role": role, "content": text[:1500]})
    return out[-max_turns:]


def _requires_pir(intents: set[str]) -> bool:
    return bool(intents & {"who", "timeline", "metrics", "detail"})


# ---------------------------------------------------------------------------
# Loop state
# ---------------------------------------------------------------------------


@dataclass
class _LoopState:
    intents: set[str]
    mi_id: Optional[str]
    pir_called: bool = False
    pir_narrative: str = ""
    personnel_hint: list[str] = field(default_factory=list)
    pir_attached: bool = False
    # How many times we already injected a gate correction
    gate_corrections: int = 0


@dataclass
class AgentResult:
    answer: str
    route: str = "llm-mcp"
    steps: int = 0


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


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
    specs = enabled_tool_specs(dispatcher) + [SUBMIT_REPORT_TOOL]

    intents = _classify_intent(query)
    mi_id = _extract_mi_id(query)
    state = _LoopState(intents=intents, mi_id=mi_id)

    system_prompt = build_system_prompt()

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    # Inject prior Slack thread turns so follow-ups have context (e.g. "the 6
    # incidents you just listed", "give me details on that one"). Without this,
    # each question is interpreted standalone and the agent can contradict its
    # own earlier answer.
    for turn in _normalize_thread(thread_messages):
        messages.append(turn)

    messages.append({"role": "user", "content": query})

    try:
        return _native_tool_loop(llm, dispatcher, specs, messages, max_steps, state)
    except ToolsUnsupported:
        logger.info("Gateway lacks tool-calling; using JSON-planner fallback")
        return _json_planner_loop(llm, dispatcher, specs, query, max_steps, state)


# ---------------------------------------------------------------------------
# Native tool-calling path
# ---------------------------------------------------------------------------


def _native_tool_loop(
    llm: LLMClient,
    dispatcher: FreshserviceToolDispatcher,
    specs: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    max_steps: int,
    state: _LoopState,
) -> AgentResult:
    last_text = ""
    for step in range(1, max_steps + 1):
        message = llm.chat(messages, tools=specs, tool_choice="auto")
        tool_calls = message.get("tool_calls") or []
        content = (message.get("content") or "").strip()
        if content:
            last_text = content

        if not tool_calls:
            forced = _force_submit_report(llm, specs, messages, content, state)
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
                # PIR gate: if detail-intent on known ticket and PIR not yet called, intercept
                gate_msg = _check_pir_gate(state, args, max_corrections=1)
                if gate_msg:
                    # Inject a corrective assistant message and continue
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": gate_msg,
                        }
                    )
                    state.gate_corrections += 1
                    logger.info("PIR gate fired for %s — injecting correction", state.mi_id)
                    break  # restart loop iteration
                # Submit accepted — check if retry needed for who/timeline
                rendered = render_report(args)
                retry = _maybe_retry(llm, specs, messages, args, state, step)
                return AgentResult(
                    answer=retry if retry else rendered,
                    route="llm-mcp",
                    steps=step,
                )

            # Track get_pir calls
            if name == "get_pir":
                result = dispatcher.call_tool(name, args)
                _capture_pir_state(state, result)
                # Send a compact version to the LLM — raw_bridge_notes is large and
                # redundant with pir_narrative; only the LLM-extracted fields are needed
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": _result_to_text(_pir_compact(result)),
                    }
                )
            else:
                result = dispatcher.call_tool(name, args)
                if name in ("search_tickets", "list_major_incidents"):
                    _try_extract_mi_from_result(state, result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": _result_to_text(result),
                    }
                )

    forced = _force_submit_report(llm, specs, messages, last_text, state)
    if forced is not None:
        return AgentResult(answer=forced, route="llm-mcp", steps=max_steps)
    return AgentResult(
        answer=last_text or "_No answer produced._", route="llm-mcp", steps=max_steps
    )


# ---------------------------------------------------------------------------
# Gate + retry helpers
# ---------------------------------------------------------------------------


def _check_pir_gate(state: _LoopState, submit_args: dict, max_corrections: int = 1) -> Optional[str]:
    """Return a corrective message if submit_report is premature; else None."""
    if not _requires_pir(state.intents):
        return None
    if state.pir_called:
        return None
    if state.gate_corrections >= max_corrections:
        return None
    ticket_id = state.mi_id
    if not ticket_id:
        # Try to extract from already submitted headline / key_facts
        headline = submit_args.get("headline", "") or ""
        m = re.search(r"\b(\d{7})\b", headline)
        if m:
            ticket_id = m.group(1)
        else:
            return None  # no ticket to look up
    return (
        f"GATE: You must call get_pir(ticket_id=\"{ticket_id}\") before submitting the report "
        f"for this incident. The user is asking about {sorted(state.intents)} — all of these "
        "require reading the Post Incident Report first. Call get_pir now, then submit_report."
    )


def _maybe_retry(
    llm: LLMClient,
    specs: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    submitted_args: dict,
    state: _LoopState,
    step: int,
) -> Optional[str]:
    """
    One-shot retry: if intent=who and personnel is empty in the submitted report
    but the captured PIR narrative has names, re-prompt the LLM to extract them.
    Same for intent=timeline.
    """
    retry_intents: list[str] = []

    if "who" in state.intents:
        personnel = submitted_args.get("personnel") or []
        if not personnel and state.pir_narrative and state.personnel_hint:
            retry_intents.append("who")

    if "timeline" in state.intents:
        timeline = submitted_args.get("timeline") or []
        if not timeline and state.pir_narrative:
            retry_intents.append("timeline")

    if not retry_intents:
        return None

    # Build a focused re-prompt
    prompt_parts = ["The user asked about: " + ", ".join(retry_intents) + "."]
    if "who" in retry_intents:
        hint_str = ", ".join(state.personnel_hint[:20]) if state.personnel_hint else "(none)"
        prompt_parts.append(
            f"The PIR narrative is attached below. Extract EVERY named person (with their role if stated). "
            f"Hint — names detected: {hint_str}. "
            "Populate submit_report.personnel with ALL of them."
        )
    if "timeline" in retry_intents:
        prompt_parts.append(
            "Also extract every timestamped event from the PIR narrative and populate "
            "submit_report.timeline as a chronological list of strings in format 'HH:MM — event'."
        )
    prompt_parts.append(
        "\n\nPIR narrative:\n" + state.pir_narrative[:6000]
        if state.pir_narrative
        else ""
    )
    prompt_parts.append(
        "\n\nPrevious submit_report args for context:\n" + json.dumps(submitted_args, default=str)[:2000]
    )
    prompt_parts.append(
        "\nNow call submit_report again. Keep the same `answer` text, and add the corrected "
        "personnel and/or timeline fields."
    )

    retry_messages = list(messages) + [
        {"role": "user", "content": "\n".join(prompt_parts)}
    ]
    try:
        retry_msg = llm.chat(
            retry_messages,
            tools=specs,
            tool_choice={"type": "function", "function": {"name": SUBMIT_REPORT_NAME}},
        )
    except Exception as exc:
        logger.info("One-shot retry failed (%s); using original report", exc)
        return None

    for call in retry_msg.get("tool_calls") or []:
        if (call.get("function") or {}).get("name") == SUBMIT_REPORT_NAME:
            args = _safe_json((call.get("function") or {}).get("arguments") or "{}")
            logger.info(
                "One-shot retry: personnel=%s timeline=%s",
                args.get("personnel"),
                len(args.get("timeline") or []),
            )
            return render_report(args)
    return None


def _capture_pir_state(state: _LoopState, result: ToolResult) -> None:
    state.pir_called = True
    data = result.data if isinstance(result.data, dict) else {}
    state.pir_narrative = data.get("pir_narrative") or ""
    state.personnel_hint = data.get("personnel_hint") or data.get("personnel") or []
    state.pir_attached = bool(data.get("pir_attached"))
    if not state.mi_id:
        state.mi_id = str(data.get("ticket_id") or "")


def _try_extract_mi_from_result(state: _LoopState, result: ToolResult) -> None:
    if state.mi_id:
        return
    try:
        text = json.dumps(result.data, default=str)
        m = _MI_ID_RE.search(text)
        if m:
            state.mi_id = m.group(1)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Forced submit_report (no tool calls produced)
# ---------------------------------------------------------------------------


def _force_submit_report(
    llm: LLMClient,
    specs: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    content: str,
    state: _LoopState,
) -> Optional[str]:
    msgs = list(messages)
    if content:
        msgs.append({"role": "assistant", "content": content})
    msgs.append(
        {
            "role": "user",
            "content": (
                "Deliver the final answer now using the submit_report tool call. "
                "Put a precise, conversational reply to exactly what the user asked in `answer`. "
                "Only add optional sections (key_facts, timeline, personnel, links) if the "
                "question calls for them — for a focused question, just fill `answer`. "
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
            rendered = render_report(args)
            # Still do one-shot retry if needed
            retry = _maybe_retry(llm, specs, msgs, args, state, 0)
            return retry if retry else rendered
    return None


# ---------------------------------------------------------------------------
# JSON-planner fallback
# ---------------------------------------------------------------------------


def _json_planner_loop(
    llm: LLMClient,
    dispatcher: FreshserviceToolDispatcher,
    specs: list[dict[str, Any]],
    query: str,
    max_steps: int,
    state: _LoopState,
) -> AgentResult:
    tool_catalog = "\n".join(
        f"- {s['function']['name']}: {s['function']['description']}" for s in specs
    )
    planner_system = (
        build_system_prompt()
        + "\n\nYou do NOT have native tool calling. Each turn respond with ONLY a JSON object:\n"
        '{"tool": <tool name>, "args": {<arguments>}}\n'
        "Call tools to gather data. When done, set tool=\"submit_report\" and put a precise, "
        "conversational reply in args.answer (this is the primary field). Only add optional "
        "sections (key_facts, timeline, personnel, links) when the question calls for them. "
        "timeline = list of 'HH:MM — event' strings from the PIR; "
        "personnel = list of every named person with role from the PIR.\n\n"
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
            # Check PIR gate
            gate_msg = _check_pir_gate(state, args, max_corrections=1)
            if gate_msg:
                observations.append(f"[gate] {gate_msg}")
                state.gate_corrections += 1
                continue
            rendered = render_report(args)
            retry = _maybe_retry(llm, specs, [], args, state, step)
            return AgentResult(
                answer=retry if retry else rendered,
                route="llm-mcp-json",
                steps=step,
            )
        if not tool:
            answer = (plan.get("answer") or "").strip()
            if answer:
                return AgentResult(answer=answer, route="llm-mcp-json", steps=step)
            break

        if tool == "get_pir":
            result = dispatcher.call_tool(tool, args)
            _capture_pir_state(state, result)
        else:
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
            args = final.get("args") or {}
            rendered = render_report(args)
            retry = _maybe_retry(llm, specs, [], args, state, max_steps)
            return AgentResult(
                answer=retry if retry else rendered,
                route="llm-mcp-json",
                steps=max_steps,
            )
    except Exception as exc:
        logger.warning("Final forced submit_report failed: %s", exc)

    return AgentResult(
        answer="\n".join(observations) or "_No data retrieved._",
        route="llm-mcp-json",
        steps=max_steps,
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _pir_compact(result: ToolResult) -> ToolResult:
    """Return a copy of a get_pir result with large redundant fields stripped.

    raw_bridge_notes is redundant with pir_narrative (same text, different shape).
    Removing it cuts the tool-result payload by 30-40% without losing information
    because the LLM reads pir_narrative for extraction and timeline_events for structured
    timeline data.
    """
    data = result.data if isinstance(result.data, dict) else result.data
    if not isinstance(data, dict):
        return result
    slim = {k: v for k, v in data.items() if k != "raw_bridge_notes"}
    # Also cap pir_narrative at 3000 chars — enough for name/role extraction
    if "pir_narrative" in slim and isinstance(slim["pir_narrative"], str):
        slim["pir_narrative"] = slim["pir_narrative"][:3000]
    from agents.freshservice.mcp_server import ToolResult as TR
    return TR(data=slim)


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
