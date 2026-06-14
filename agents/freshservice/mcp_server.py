"""
MCP tool layer for the Freshservice agent.

Exposes Freshservice, Freshstatus, and MySQL operations as OpenAI-compatible
tool specs. The LLM agent loop calls these to gather evidence before replying.
All tools are read-only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from agents.freshservice.freshservice_client import (
    FreshserviceClient,
    FreshserviceUnavailable,
)
from agents.freshservice.freshstatus_client import FreshstatusClient
from agents.freshservice.mysql_client import MySQLClient
from agents.freshservice.pg_store import PgStore
from agents.freshservice.slack_client import SlackOutageClient
from agents.freshservice.context_resolver import mi_ref_to_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool specs (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_ticket",
            "description": (
                "Fetch a Freshservice ticket by its numeric id or MI-reference "
                "(e.g. MI-4301250 or plain 4301250). Returns ticket fields including "
                "status, priority, assignee, custom_fields, and operational_metrics."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "Numeric id or 'MI-XXXXXXX' reference.",
                    }
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ticket_conversations",
            "description": "Fetch recent notes/conversations for a Freshservice ticket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max conversations (default 50)."},
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pir",
            "description": (
                "Fetch the full Post Incident Report (PIR) for a MIM ticket. "
                "Returns: mtta_minutes, mttd_minutes, mttr_minutes, start/end times, "
                "products_affected, regions_affected, issue_category, impact_to_customer, "
                "pir_status (Draft/Published), pir_url, "
                "pir_narrative (full chronological bridge-note text — PRIMARY source for "
                "extracting who was involved and what happened step-by-step), "
                "personnel_hint (best-effort list of names found in bridge notes), "
                "pir_attached (bool — False if no bridge notes exist), "
                "timeline_events (list of {time, event} dicts from bridge timestamps). "
                "ALWAYS call this first for questions about incident details, "
                "timeline, root cause, MTTR/MTTD, impact, or 'who was involved'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "Freshservice ticket id or MI-reference (e.g. MI-4381855 or 4381855).",
                    }
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tickets",
            "description": (
                "Search Freshservice MIM/Major Incident tickets by product, date, keyword or free text. "
                "Use this for 'recent Freshdesk outages', 'latest incidents for Freshchat', "
                "'show me recent MIM tickets', 'outage on May 14', 'incident last week', "
                "or any question about multiple incidents when MySQL is not available. "
                "Returns list of tickets with subject, status, priority, incident_start_time, "
                "MTTR, products_affected, pir_url etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Product or keyword filter (e.g. 'Freshdesk', 'performance'). Leave empty for all recent MIM tickets."},
                    "date_filter": {"type": "string", "description": "Filter by a specific incident date, e.g. 'May 14', '2026-05-14', 'June 4'. Matches against incident_start_time."},
                    "issue_category": {"type": "string", "description": "Filter by root-cause category: 'Third-party', 'Infra', 'Code', 'Database', 'Deployment', or 'Configuration'. Synonym-aware (e.g. 'vendor'/'external' map to Third-party)."},
                    "months_back": {"type": "integer", "description": "Only include incidents within the last N months (e.g. 1 = last month, 6 = last 6 months)."},
                    "per_page": {"type": "integer", "description": "Max results (default 15, max 30)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_freshstatus_incidents",
            "description": (
                "Fetch Freshstatus public incidents for Freshworks (account 65). "
                "Use for 'active incidents', 'any current outages', or 'Freshstatus status'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "active_only": {
                        "type": "boolean",
                        "description": "If true, return only unresolved incidents.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_ongoing_outages",
            "description": (
                "Authoritative check for ANY ongoing/active/current outage RIGHT NOW. "
                "Returns BOTH (a) Freshstatus open/unresolved public incidents AND "
                "(b) recent messages from the internal fw-outage Slack channel. "
                "ALWAYS use this for 'is there any ongoing outage', 'any active incident', "
                "'current outages', or leadership 'are we down right now' questions — it "
                "confirms across the public status page and internal Slack chatter."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Look-back window for fw-outage Slack messages (default 24).",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_outages",
            "description": (
                "Query the Outages_data MySQL table (internal NOC incident records). "
                "Returns rows sorted newest-first. Filter by product or incident_no."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max rows (default 20)."},
                    "product": {"type": "string", "description": "Product name filter (e.g. 'Freshdesk')."},
                    "incident_no": {"type": "string", "description": "Internal incident number."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_mim_tickets",
            "description": (
                "Query MIM_Ticket_id table — links Freshservice ticket ids to "
                "internal incidents. Includes assignee, Slack thread, product, region."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max rows (default 50)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_update_threads",
            "description": "Query All_update_threads — Slack thread rows for an incident.",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_no": {"type": "string", "description": "Internal incident number."},
                    "limit": {"type": "integer", "description": "Max rows (default 18)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_mim_analytics",
            "description": (
                "Query MIM_Analytics_export — aggregated MIM ticket data from "
                "Freshservice Analytics. Use for period counts, cause segments, trends."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max rows (default 200)."},
                    "product": {"type": "string", "description": "Product name filter."},
                },
            },
        },
    },
]

TOOL_NAMES = {spec["function"]["name"] for spec in TOOL_SPECS}

# Tools that require the legacy MySQL store. When MYSQL_HOST is unset these are
# dead weight: they bloat the prompt (slowing every LLM call) and tempt the model
# into calling tools that always error. They are filtered out at runtime.
_MYSQL_TOOLS = {
    "query_outages",
    "query_mim_tickets",
    "query_update_threads",
    "query_mim_analytics",
}


def enabled_tool_specs(dispatcher: "FreshserviceToolDispatcher") -> list[dict[str, Any]]:
    """Return only the tool specs whose backing service is configured.

    Sending the LLM tools it cannot use makes every call slower (more input
    tokens) and less accurate (the model may pick a dead tool). We expose the
    MySQL tools only when the MySQL store is actually enabled.
    """
    mysql_on = getattr(dispatcher.db, "enabled", False)
    specs: list[dict[str, Any]] = []
    for spec in TOOL_SPECS:
        name = spec["function"]["name"]
        if name in _MYSQL_TOOLS and not mysql_on:
            continue
        specs.append(spec)
    return specs


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    data: Any


class FreshserviceToolDispatcher:
    def __init__(
        self,
        fs_client: FreshserviceClient,
        status_client: FreshstatusClient,
        db_client: MySQLClient,
        pg_store: Optional[PgStore] = None,
        slack_client: Optional[SlackOutageClient] = None,
    ):
        self.fs = fs_client
        self.status = status_client
        self.db = db_client
        self.pg = pg_store or PgStore()
        self.slack = slack_client or SlackOutageClient()

    def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        args = args or {}
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return ToolResult(data={"error": "unknown_tool", "tool": name})
        try:
            return ToolResult(data=handler(args))
        except Exception as exc:
            logger.warning("Tool %s failed: %s", name, exc)
            return ToolResult(data={"error": "tool_failed", "tool": name, "message": str(exc)})

    def _tool_get_ticket(self, args: dict) -> Any:
        if not self.fs.enabled:
            return {"error": "Freshservice API key not configured (FRESHSERVICE_API_KEY unset)."}
        raw_id = str(args.get("ticket_id") or "")
        tid = mi_ref_to_id(raw_id) or raw_id
        try:
            return self.fs.get_ticket(tid)
        except FreshserviceUnavailable as exc:
            cached = self.pg.store_get_ticket(tid) if self.pg.enabled else {}
            if cached:
                cached["source"] = "pg_fallback"
                return cached
            return {"error": f"Freshservice unavailable ({exc}); no cached copy in MIM_Store.", "ticket_id": tid}

    def _tool_get_ticket_conversations(self, args: dict) -> Any:
        if not self.fs.enabled:
            return {"error": "Freshservice API key not configured."}
        raw_id = str(args.get("ticket_id") or "")
        tid = mi_ref_to_id(raw_id) or raw_id
        limit = int(args.get("limit") or 50)
        convs = self.fs.get_ticket_conversations(tid, limit=limit)
        return {"ticket_id": tid, "conversations": convs}

    def _tool_get_pir(self, args: dict) -> Any:
        if not self.fs.enabled:
            return {"error": "Freshservice API key not configured (FRESHSERVICE_API_KEY unset)."}
        raw_id = str(args.get("ticket_id") or "")
        tid = mi_ref_to_id(raw_id) or raw_id
        try:
            return self.fs.get_pir(tid)
        except FreshserviceUnavailable as exc:
            cached = self.pg.store_get_pir(tid) if self.pg.enabled else {}
            if cached:
                cached["source"] = "pg_fallback"
                return cached
            return {"error": f"Freshservice unavailable ({exc}); no cached PIR in MIM_Store.", "ticket_id": tid}

    def _tool_search_tickets(self, args: dict) -> Any:
        if not self.fs.enabled:
            return {"error": "Freshservice API key not configured."}
        issue_category = args.get("issue_category") or ""
        product = args.get("query") or ""
        months_back = int(args.get("months_back") or 0)
        try:
            tickets = self.fs.search_tickets(
                query=product,
                per_page=int(args.get("per_page") or 15),
                date_filter=args.get("date_filter") or "",
                issue_category=issue_category,
                months_back=months_back,
            )
            return {"count": len(tickets), "tickets": tickets}
        except FreshserviceUnavailable as exc:
            return self._store_list_fallback(issue_category, product, months_back,
                                             int(args.get("per_page") or 15), exc)

    def _tool_list_major_incidents(self, args: dict) -> Any:
        if not self.fs.enabled:
            return {"error": "Freshservice API key not configured."}
        issue_category = args.get("issue_category") or ""
        product = args.get("product") or ""
        months_back = int(args.get("months_back") or 0)
        limit = int(args.get("limit") or 20)
        try:
            tickets = self.fs.list_major_incidents(
                product_filter=product,
                limit=limit,
                date_filter=args.get("date_filter") or "",
                issue_category=issue_category,
                months_back=months_back,
            )
            return {"count": len(tickets), "tickets": tickets}
        except FreshserviceUnavailable as exc:
            return self._store_list_fallback(issue_category, product, months_back, limit, exc)

    def _store_list_fallback(
        self,
        issue_category: str,
        product: str,
        months_back: int,
        limit: int,
        exc: Exception,
    ) -> Any:
        """Serve a MIM list from the Postgres cache when the live API is unavailable."""
        if self.pg.enabled:
            rows = self.pg.store_list(
                issue_category=issue_category,
                product=product,
                months_back=months_back,
                limit=limit,
            )
            if rows:
                return {"count": len(rows), "tickets": rows, "source": "pg_fallback"}
        return {
            "error": f"Freshservice unavailable ({exc}); no matching rows in MIM_Store cache.",
            "count": 0,
            "tickets": [],
        }

    def _tool_get_freshstatus_incidents(self, args: dict) -> Any:
        active_only = bool(args.get("active_only", False))
        if active_only:
            active = self.status.get_active_incidents()
            return {"active_count": len(active), "active": active}
        active = self.status.get_active_incidents()
        recent = self.status.get_recent_incidents(limit=20)
        return {
            "active_count": len(active),
            "active": active,
            "recent_count": len(recent),
            "recent": recent,
        }

    def _tool_check_ongoing_outages(self, args: dict) -> Any:
        """Confirm ongoing outages across Freshstatus AND the fw-outage Slack channel."""
        hours = int(args.get("hours") or 24)
        active = self.status.get_active_incidents()
        slack_msgs = self.slack.get_recent_messages(hours=hours) if self.slack.enabled else []
        slack_available = self.slack.enabled
        ongoing = bool(active) or bool(slack_msgs)
        return {
            "ongoing_outage": ongoing,
            "freshstatus_active_count": len(active),
            "freshstatus_active": active,
            "fw_outage_slack_available": slack_available,
            "fw_outage_recent_count": len(slack_msgs),
            "fw_outage_recent_messages": slack_msgs,
            "note": (
                "fw-outage Slack not configured (no token/channel or bot not in channel)."
                if not slack_available else
                "Checked both Freshstatus public incidents and internal fw-outage Slack chatter."
            ),
        }

    def _tool_query_outages(self, args: dict) -> Any:
        if not self.db.enabled:
            return {
                "error": "MySQL not configured (MYSQL_HOST unset). "
                "Only Freshservice API and Freshstatus data are available.",
                "rows": [],
            }
        rows = self.db.query_outages(
            limit=int(args.get("limit") or 20),
            product=args.get("product"),
            incident_no=args.get("incident_no"),
        )
        return {"count": len(rows), "rows": rows}

    def _tool_query_mim_tickets(self, args: dict) -> Any:
        if not self.db.enabled:
            return {"error": "MySQL not configured.", "rows": []}
        rows = self.db.query_mim_tickets(limit=int(args.get("limit") or 50))
        return {"count": len(rows), "rows": rows}

    def _tool_query_update_threads(self, args: dict) -> Any:
        if not self.db.enabled:
            return {"error": "MySQL not configured.", "rows": []}
        rows = self.db.query_update_threads(
            limit=int(args.get("limit") or 18),
            incident_no=args.get("incident_no"),
        )
        return {"count": len(rows), "rows": rows}

    def _tool_query_mim_analytics(self, args: dict) -> Any:
        if not self.db.enabled:
            return {"error": "MySQL not configured.", "rows": []}
        rows = self.db.query_mim_analytics(
            limit=int(args.get("limit") or 200),
            product=args.get("product"),
        )
        return {"count": len(rows), "rows": rows}


def build_mcp_server(dispatcher: Optional[FreshserviceToolDispatcher] = None):
    """Build a FastMCP server exposing the same tools. Returns None if mcp SDK absent."""
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:
        logger.info("mcp SDK not available (%s); using in-process dispatcher only", exc)
        return None

    if dispatcher is None:
        return None

    server = FastMCP("freshservice-agent")

    for spec in TOOL_SPECS:
        fn = spec["function"]
        tool_name = fn["name"]

        async def _tool(_disp=dispatcher, _name=tool_name, **kwargs: Any) -> Any:
            return _disp.call_tool(_name, kwargs).data

        _tool.__name__ = tool_name
        server.tool(name=tool_name, description=fn.get("description", ""))(_tool)

    return server
