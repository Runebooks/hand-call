#!/usr/bin/env python3
"""
Leadership-grade accuracy harness for the freshservice-agent.

Runs a battery of realistic leadership questions through the FULL LLM agent loop
(same path as Slack), records which tools were called, and applies a per-question
pass/fail heuristic. Prints a summary accuracy table.

Usage (needs FRESHSERVICE_API_KEY + LLM creds in env; best run in-cluster):
  python scripts/test-freshservice-questions.py
  python scripts/test-freshservice-questions.py --only third-party,ongoing
  python scripts/test-freshservice-questions.py --tools-only   # skip LLM, sanity-check tools

Exit code is non-zero if the pass rate is below --min-pass (default 0.7).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Each case: id, question, expect_any (>=1 must appear), expect_all (ALL must appear),
# forbid (any → fail), expect_tools (>=1 of these tools should run).
CASES = [
    # ── Personnel / who ───────────────────────────────────────────────────────
    {
        "id": "who-involved",
        "q": "Who was involved in the Freshdesk outage on May 14?",
        # Must include Sunil Sakshi + Sarvani AND at least one of Dharshini/Akhilesh
        "expect_all": ["sunil sakshi", "sarvani"],
        "expect_any": ["dharshini", "akhilesh"],
        "forbid": ["not found", "not available", "cannot be confirmed", "no pir"],
        "expect_tools": ["get_pir"],
    },
    {
        "id": "who-responders",
        "q": "Who responded to MI-4381855?",
        "expect_any": ["akhilesh", "dharshini", "sunil", "sarvani", "divyanshu", "palani"],
        "forbid": ["not found", "not available", "no pir"],
        "expect_tools": ["get_pir"],
    },
    {
        "id": "mim-assignee",
        "q": "Who was the MIM assignee for MI-4381855?",
        "expect_any": ["dharshini", "sunil", "assignee", "sarvani"],
        "forbid": ["not available", "could not"],
        "expect_tools": ["get_pir", "get_ticket"],
    },
    {
        "id": "rca-owner",
        "q": "Who owned the RCA for the Freshdesk export incident on May 14?",
        "expect_any": ["rca", "sarvani", "sunil", "dharshini", "akhilesh", "owner"],
        "forbid": ["not available", "not found"],
        "expect_tools": ["get_pir"],
    },

    # ── Timeline / sequence ───────────────────────────────────────────────────
    {
        "id": "timeline",
        "q": "Show me the incident timeline for MI-4381855",
        # Must mention rollback and a timestamp near 11:42 (resolved)
        "expect_all": ["rollback"],
        "expect_any": ["11:42", "11:08", "9:42", "akhilesh", "bridge"],
        "forbid": ["not available", "could not", "no pir"],
        "expect_tools": ["get_pir"],
    },
    {
        "id": "what-happened",
        "q": "What happened during MI-4381855?",
        "expect_any": ["export", "sql", "freshdesk", "degradation", "rollback"],
        "forbid": ["not available", "could not"],
        "expect_tools": ["get_pir"],
    },
    {
        "id": "walkthrough",
        "q": "Walk me through the Freshdesk export incident on May 14",
        "expect_any": ["sql", "rollback", "9:00", "11:42", "export", "akhilesh"],
        "forbid": ["not available", "not found"],
        "expect_tools": ["get_pir"],
    },

    # ── Metrics ───────────────────────────────────────────────────────────────
    {
        "id": "mttr",
        "q": "What is the MTTR for MI-4381855?",
        "expect_all": ["552"],
        "expect_any": ["minute", "hour", "mttr"],
        "forbid": ["not available", "could not"],
        "expect_tools": ["get_pir", "get_ticket"],
    },
    {
        "id": "mttd-mtta",
        "q": "What were the MTTD and MTTA for MI-4381855?",
        "expect_any": ["432", "435", "mttd", "mtta", "detect", "acknowledge"],
        "forbid": ["not available", "could not"],
        "expect_tools": ["get_pir", "get_ticket"],
    },
    {
        "id": "duration",
        "q": "How long did the Freshdesk outage on May 14 last?",
        "expect_any": ["552", "9 h", "hours", "9:00", "18:12"],
        "forbid": ["not available", "could not"],
        "expect_tools": ["get_pir", "get_ticket"],
    },

    # ── Root cause / impact ───────────────────────────────────────────────────
    {
        "id": "root-cause",
        "q": "What was the root cause of MI-4381855?",
        "expect_any": ["infra", "sql", "export", "performance", "degradation", "rollback"],
        "forbid": ["not available", "could not"],
        "expect_tools": ["get_pir"],
    },
    {
        "id": "impact",
        "q": "What was the customer impact of MI-4381855?",
        "expect_any": ["export", "unable", "freshdesk", "account", "list-view"],
        "forbid": ["not available", "could not"],
        "expect_tools": ["get_pir", "get_ticket"],
    },
    {
        "id": "regions",
        "q": "Which regions were affected in MI-4381855?",
        "expect_all": ["us"],
        "expect_any": ["eun", "au", "region"],
        "forbid": ["not available", "could not"],
        "expect_tools": ["get_pir", "get_ticket"],
    },

    # ── Issue category / filtering ────────────────────────────────────────────
    {
        "id": "third-party-month",
        "q": "Can you get the MIMs related to third-party in the last one month?",
        "expect_any": ["third", "vendor", "cloudflare", "whatsapp", "instagram", "aws", "third-party"],
        "forbid": ["not available", "could not find", "no matching", "i don't have"],
        "expect_tools": ["search_tickets"],
    },
    {
        "id": "infra-6mo",
        "q": "How many infra-related incidents happened in the last 6 months?",
        "expect_any": ["infra"],
        "forbid": ["not available", "no matching"],
        "expect_tools": ["search_tickets"],
    },
    {
        "id": "code-incidents",
        "q": "Show me code-caused incidents from the last 3 months",
        "expect_any": ["code", "incident", "mim", "ticket"],
        "forbid": ["not available", "no matching"],
        "expect_tools": ["search_tickets"],
    },

    # ── PIR / status ──────────────────────────────────────────────────────────
    {
        "id": "pir-status",
        "q": "Is the PIR for MI-4381855 published?",
        "expect_any": ["published", "pir", "post incident"],
        "forbid": ["not available", "could not"],
        "expect_tools": ["get_pir"],
    },
    {
        "id": "pir-attached",
        "q": "Does MI-4381855 have a PIR attached?",
        "expect_any": ["yes", "pir", "published", "post incident", "attached"],
        "forbid": ["not available"],
        "expect_tools": ["get_pir"],
    },

    # ── Ongoing outage ────────────────────────────────────────────────────────
    {
        "id": "ongoing",
        "q": "Is there any ongoing outage right now?",
        "expect_any": ["no active", "ongoing", "freshstatus", "fw-outage", "active", "no outage"],
        "forbid": [],
        "expect_tools": ["check_ongoing_outages"],
    },

    # ── Briefing ──────────────────────────────────────────────────────────────
    {
        "id": "recent-freshdesk",
        "q": "Give me a briefing on recent Freshdesk outages",
        "expect_any": ["freshdesk"],
        "forbid": ["mysql", "not available from this environment"],
        "expect_tools": ["search_tickets"],
    },
    {
        "id": "cloudflare",
        "q": "Was there a Cloudflare-related incident recently?",
        "expect_any": ["cloudflare", "third", "incident", "mim", "vendor"],
        "forbid": [],
        "expect_tools": ["search_tickets"],
    },
]


class _RecordingDispatcher:
    """Wraps the real dispatcher to record tool-call names."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[str] = []

    def call_tool(self, name, args):
        self.calls.append(name)
        return self._inner.call_tool(name, args)

    def __getattr__(self, item):
        return getattr(self._inner, item)


def _build_dispatcher():
    from agents.freshservice.freshservice_client import FreshserviceClient
    from agents.freshservice.freshstatus_client import FreshstatusClient
    from agents.freshservice.mysql_client import MySQLClient
    from agents.freshservice.pg_store import PgStore
    from agents.freshservice.slack_client import SlackOutageClient
    from agents.freshservice.mcp_server import FreshserviceToolDispatcher

    return FreshserviceToolDispatcher(
        FreshserviceClient(), FreshstatusClient(), MySQLClient(), PgStore(), SlackOutageClient()
    )


def _judge(case: dict, answer: str, tools: list[str]) -> tuple[bool, str]:
    low = (answer or "").lower()
    if not low.strip():
        return False, "empty answer"
    for bad in case.get("forbid", []):
        if bad.lower() in low:
            return False, f"forbidden phrase: '{bad}'"
    # expect_all: every item must appear
    for required in case.get("expect_all", []):
        if required.lower() not in low:
            return False, f"missing required content: '{required}'"
    # expect_any: at least one must appear
    expect_any = case.get("expect_any") or []
    if expect_any and not any(e.lower() in low for e in expect_any):
        return False, f"missing expected content (any of {expect_any})"
    expect_tools = case.get("expect_tools") or []
    if expect_tools and not any(t in tools for t in expect_tools):
        return False, f"expected one of tools {expect_tools}, got {tools}"
    return True, "ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma-separated case-id substrings to run")
    ap.add_argument("--tools-only", action="store_true", help="skip LLM; just exercise data tools")
    ap.add_argument("--min-pass", type=float, default=0.7)
    args = ap.parse_args()

    base = _build_dispatcher()

    cases = CASES
    if args.only:
        wants = [s.strip() for s in args.only.split(",") if s.strip()]
        cases = [c for c in CASES if any(w in c["id"] for w in wants)]

    llm = None
    if not args.tools_only:
        from common.llm import LLMClient

        llm = LLMClient()

    if args.tools_only or not (llm and llm.enabled()):
        print("=== TOOLS-ONLY sanity (no LLM) ===")
        fs_ok = base.fs.enabled
        print(f"Freshservice enabled: {fs_ok}")
        r = base.call_tool("search_tickets", {"issue_category": "third-party", "months_back": 1})
        print(f"third-party last month: count={r.data.get('count')}")
        r2 = base.call_tool("get_pir", {"ticket_id": "4381855"})
        d2 = r2.data if isinstance(r2.data, dict) else {}
        print(f"PIR 4381855: pir_attached={d2.get('pir_attached')} timeline_events={len(d2.get('timeline_events', []))} personnel_hint={len(d2.get('personnel_hint', []))} pir_narrative_len={len(d2.get('pir_narrative', ''))}")
        r3 = base.call_tool("check_ongoing_outages", {})
        print(f"ongoing: fs_active={r3.data.get('freshstatus_active_count')} slack_available={r3.data.get('fw_outage_slack_available')}")
        return 0

    from agents.freshservice.agent_loop import run_agent_loop

    passed = 0
    print(f"=== Running {len(cases)} leadership questions through full agent loop ===\n")
    for c in cases:
        rec = _RecordingDispatcher(base)
        t0 = time.time()
        try:
            result = run_agent_loop(c["q"], dispatcher=rec, llm=llm, max_steps=8)
            answer = result.answer
        except Exception as exc:
            answer = f"__EXCEPTION__ {exc}"
        dt = time.time() - t0
        ok, reason = _judge(c, answer, rec.calls)
        passed += 1 if ok else 0
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {c['id']} ({dt:.1f}s) tools={rec.calls}")
        if not ok:
            print(f"        reason: {reason}")
        print(f"        Q: {c['q']}")
        print(f"        A: {answer[:280].replace(chr(10), ' ')}")
        print()

    rate = passed / len(cases) if cases else 0.0
    print(f"=== Accuracy: {passed}/{len(cases)} = {rate:.0%} ===")
    return 0 if rate >= args.min_pass else 2


if __name__ == "__main__":
    sys.exit(main())
