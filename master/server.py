"""
Master agent — standalone A2A server (same pattern as specialist agents).

Slack / n8n → A2A tasks/send → AgentRegistry + AgentRouter → specialist agents.
LLM used only when alertname + keyword routing are ambiguous (Layer 3).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.a2a_server import A2AServer
from common.models import Artifact, Task

from master.alert_metadata import alert_from_metadata, non_alert_metadata
from master.orchestrator import MasterOrchestrator

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

CARD_PATH = Path(__file__).parent / "agent_card.json"
DEFAULT_PORT = int(os.environ.get("A2A_PORT", os.environ.get("MASTER_PORT", "8095")))


class MasterAgent(A2AServer):
    """A2A router/orchestrator — routes NOC investigations to specialist agents."""

    def __init__(self, **kwargs):
        super().__init__(agent_card_path=str(CARD_PATH), **kwargs)
        self.orchestrator = MasterOrchestrator()
        self._register_ops_routes()

    def _register_ops_routes(self) -> None:
        """Optional REST helpers for ops (Agent Card discovery summary)."""

        @self.app.get("/agents")
        async def list_agents() -> dict[str, Any]:
            agents = self.orchestrator.registry.refresh()
            catalog = []
            for name, reg in agents.items():
                catalog.append(
                    {
                        "name": name,
                        "url": reg.url,
                        "description": reg.card.description,
                        "skills": [
                            {"id": s.id, "name": s.name, "tags": s.tags}
                            for s in reg.card.skills
                        ],
                    }
                )
            return {"agents": [entry["name"] for entry in catalog], "catalog": catalog}

    async def on_startup(self) -> None:
        try:
            agents = self.orchestrator.list_agents()
            logger.info(
                "Master agent started (A2A). Registered specialists: %s",
                ", ".join(agents) or "(none — check AGENT_URLS)",
            )
        except Exception as exc:
            logger.warning("Agent registry failed on startup: %s", exc)

    async def process_task(self, task: Task) -> Task:
        query = task.message.get_text() if task.message else ""
        meta = task.metadata or {}
        alert = alert_from_metadata(meta)
        session_id = task.session_id or task.id

        if not query.strip():
            task.mark_failed("Empty query — send an investigation question or alert context.")
            task.add_artifact(
                Artifact.text("Please send a question about the alert or cluster.")
            )
            return task

        try:
            task.mark_working("Routing to specialist agent…")
            result = self.orchestrator.investigate(
                query,
                alert=alert,
                session_id=session_id,
                extra_metadata=non_alert_metadata(meta),
            )
            task.metadata = {
                **meta,
                "routed_agent": result.agent.name,
                "routed_agent_url": result.agent.url,
                "routed_agent_description": result.agent.card.description,
            }
            task.add_artifact(
                Artifact.text(result.answer or "_No details returned._", name="investigation-result")
            )
            task.mark_completed()
        except Exception as exc:
            logger.exception("Master investigation failed")
            task.mark_failed(str(exc))
            task.add_artifact(Artifact.text(f"Master agent error: {exc}\n\nQuery: {query}"))
        return task


def main() -> None:
    host = os.environ.get("A2A_HOST", os.environ.get("MASTER_HOST", "0.0.0.0"))
    port = DEFAULT_PORT
    agent = MasterAgent(host=host, port=port)
    agent.run()


if __name__ == "__main__":
    main()
