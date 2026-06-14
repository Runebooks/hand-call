"""
Freshservice NOC Agent — A2A server entry point.

Listens on port 8083 (default). Receives tasks from the master agent and
routes them through the LLM tool-calling loop, which fetches data from
Freshservice, Freshstatus, and (optionally) MySQL before returning an
exec-ready Slack briefing.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.a2a_server import A2AServer
from common.models import Artifact, Task
from common.llm import LLMClient

from agents.freshservice.freshservice_client import FreshserviceClient
from agents.freshservice.freshstatus_client import FreshstatusClient
from agents.freshservice.mysql_client import MySQLClient
from agents.freshservice.pg_store import PgStore
from agents.freshservice.slack_client import SlackOutageClient
from agents.freshservice.mcp_server import FreshserviceToolDispatcher
from agents.freshservice.agent_loop import run_agent_loop, DEFAULT_MAX_STEPS

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

CARD_PATH = Path(__file__).parent / "agent_card.json"
DEFAULT_PORT = int(os.environ.get("A2A_PORT", "8083"))


class FreshserviceAgent(A2AServer):
    def __init__(self, **kwargs):
        super().__init__(agent_card_path=str(CARD_PATH), **kwargs)
        self.fs_client = FreshserviceClient()
        self.status_client = FreshstatusClient()
        self.db_client = MySQLClient()
        self.pg_store = PgStore()
        self.slack_client = SlackOutageClient()
        self.dispatcher = FreshserviceToolDispatcher(
            self.fs_client, self.status_client, self.db_client, self.pg_store, self.slack_client
        )
        self.llm = LLMClient()
        try:
            self._max_steps = int(os.environ.get("FRESHSERVICE_MCP_MAX_STEPS", str(DEFAULT_MAX_STEPS)))
        except ValueError:
            self._max_steps = DEFAULT_MAX_STEPS

    async def on_startup(self) -> None:
        if self.llm.enabled():
            logger.info("Freshservice agent ready (LLM enabled, model=%s)", self.llm.model)
        else:
            logger.warning(
                "Freshservice agent ready — LLM disabled (%s)",
                getattr(self.llm, "disabled_reason", "unknown"),
            )
        if not self.fs_client.enabled:
            logger.warning("FRESHSERVICE_API_KEY not set — Freshservice tools disabled.")
        if not self.db_client.enabled:
            logger.info("MYSQL_HOST not set — legacy MySQL tools disabled (API-only mode).")
        if self.pg_store.enabled:
            try:
                self.pg_store.ensure_schema()
                logger.info("MIM_Store (Postgres fallback) ready: %d rows cached.", self.pg_store.count())
            except Exception as exc:
                logger.warning("MIM_Store schema/connect check failed (fallback disabled): %s", exc)
        else:
            logger.info("PGHOST not set — Postgres MIM_Store fallback disabled.")
        if self.slack_client.enabled:
            logger.info("fw-outage Slack reader enabled (channel=%s).", self.slack_client.channel_id)
        else:
            logger.info("fw-outage Slack reader disabled (SLACK_BOT_TOKEN / FW_OUTAGE_SLACK_CHANNEL_ID unset).")

    async def process_task(self, task: Task) -> Task:
        query = task.message.get_text() if task.message else ""
        meta = task.metadata or {}

        if not query.strip():
            task.mark_failed("Empty query — ask about incidents, MIM tickets, or Freshstatus.")
            task.add_artifact(
                Artifact.text("Please send a question about Freshworks incidents or tickets.")
            )
            return task

        logger.info(
            "Freshservice task: %s…",
            query[:120].replace("\n", " "),
        )

        import asyncio
        loop = asyncio.get_event_loop()

        try:
            # Run the blocking LLM loop in a thread pool so the asyncio event loop
            # stays free to serve liveness/readiness health checks during long LLM calls.
            result = await loop.run_in_executor(
                None,
                lambda: run_agent_loop(
                    query,
                    metadata=meta,
                    thread_messages=meta.get("thread_messages"),
                    dispatcher=self.dispatcher,
                    llm=self.llm,
                    max_steps=self._max_steps,
                ),
            )
            answer = self._footer(result.answer, route=result.route)
            task.add_artifact(Artifact.text(answer, name="freshservice-result"))
            task.mark_completed()
        except Exception as exc:
            logger.exception("Freshservice agent failed: %s", query)
            import httpx as _httpx
            if isinstance(exc, (_httpx.ReadTimeout, _httpx.TimeoutException)):
                msg = (
                    ":hourglass_flowing_sand: The LLM gateway is responding slowly right now. "
                    "Please try again in 30–60 seconds — the underlying data tools are working fine."
                )
            else:
                msg = f"Freshservice agent error: {exc}\n\nQuery: {query}"
            task.mark_failed(str(exc))
            task.add_artifact(Artifact.text(msg))
        return task

    def _footer(self, text: str, route: str = "") -> str:
        via = f"via freshservice-agent"
        if route:
            via += f" ({route})"
        return text


if __name__ == "__main__":
    agent = FreshserviceAgent(host="0.0.0.0", port=DEFAULT_PORT)
    agent.run()
