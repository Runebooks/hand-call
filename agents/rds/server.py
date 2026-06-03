"""
RDS A2A Agent — read-only database ops (stub until RDS credentials are configured).
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

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

CARD_PATH = Path(__file__).parent / "agent_card.json"
DEFAULT_PORT = int(os.environ.get("A2A_PORT", "8081"))


class RdsAgent(A2AServer):
    def __init__(self, **kwargs):
        super().__init__(agent_card_path=str(CARD_PATH), **kwargs)
        self.rds_host = os.environ.get("RDS_HOST", "").strip()

    async def process_task(self, task: Task) -> Task:
        query = task.message.get_text() if task.message else ""
        meta = task.metadata or {}
        try:
            answer = self._handle_query(query, meta)
            task.add_artifact(Artifact.text(answer, name="rds-result"))
            task.mark_completed()
        except Exception as exc:
            logger.exception("RDS query failed")
            task.mark_failed(str(exc))
            task.add_artifact(Artifact.text(f"RDS agent error: {exc}"))
        return task

    def _handle_query(self, query: str, meta: dict) -> str:
        if not self.rds_host:
            return (
                "**RDS agent**\n\n"
                "Database access is not configured (`RDS_HOST` unset). "
                "This agent handles DB/RDS questions when credentials are added.\n\n"
                f"Question: {query[:500]}\n\n"
                "_For KubePodCrashLooping use kubernetes-agent._"
            )
        return (
            f"**RDS** (`{self.rds_host}`) configured.\n\n"
            f"Query: {query[:800]}\n\n"
            "_Read-only SQL executor can be wired to RDS_HOST next._"
        )


def main() -> None:
    host = os.environ.get("A2A_HOST", "0.0.0.0")
    port = int(os.environ.get("A2A_PORT", str(DEFAULT_PORT)))
    RdsAgent(host=host, port=port).run()


if __name__ == "__main__":
    main()
