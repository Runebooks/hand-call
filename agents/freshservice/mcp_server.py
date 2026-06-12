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

from agents.freshservice.freshservice_client import FreshserviceClient
from agents.freshservice.freshstatus_client import FreshstatusClient
from agents.freshservice.mysql_client import MySQLClient
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
                    "limit": {"type": "integer", "description": "Max conversations (default 5)."},
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tickets",
            "description": "Search Freshservice tickets by keyword query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string."},
                    "per_page": {"type": "integer", "description": "Results per page (default 10)."},
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
    ):
        self.fs = fs_client
        self.status = status_client
        self.db = db_client

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
        return self.fs.get_ticket(tid)

    def _tool_get_ticket_conversations(self, args: dict) -> Any:
        if not self.fs.enabled:
            return {"error": "Freshservice API key not configured."}
        raw_id = str(args.get("ticket_id") or "")
        tid = mi_ref_to_id(raw_id) or raw_id
        limit = int(args.get("limit") or 5)
        convs = self.fs.get_ticket_conversations(tid, limit=limit)
        return {"ticket_id": tid, "conversations": convs}

    def _tool_search_tickets(self, args: dict) -> Any:
        if not self.fs.enabled:
            return {"error": "Freshservice API key not configured."}
        tickets = self.fs.search_tickets(
            query=args.get("query") or "",
            per_page=int(args.get("per_page") or 10),
        )
        return {"count": len(tickets), "tickets": tickets}

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
