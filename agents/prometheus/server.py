"""
Prometheus A2A Agent — metrics and alert context (read-only).
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
DEFAULT_PORT = int(os.environ.get("A2A_PORT", "8080"))


class PrometheusAgent(A2AServer):
    def __init__(self, **kwargs):
        super().__init__(agent_card_path=str(CARD_PATH), **kwargs)
        self.prom_url = os.environ.get("PROMETHEUS_URL", "").strip().rstrip("/")

    async def process_task(self, task: Task) -> Task:
        query = task.message.get_text() if task.message else ""
        meta = task.metadata or {}
        try:
            answer = self._handle_query(query, meta)
            task.add_artifact(Artifact.text(answer, name="prometheus-result"))
            task.mark_completed()
        except Exception as exc:
            logger.exception("Prometheus query failed")
            task.mark_failed(str(exc))
            task.add_artifact(Artifact.text(f"Prometheus agent error: {exc}"))
        return task

    def _handle_query(self, query: str, meta: dict) -> str:
        if not self.prom_url:
            return (
                "**Prometheus agent**\n\n"
                "Prometheus is not configured (`PROMETHEUS_URL` unset). "
                "For this alert I can summarize context from the ticket:\n"
                f"- Alert: `{meta.get('alertname', 'unknown')}`\n"
                f"- Cluster: `{meta.get('cluster', 'n/a')}`\n"
                f"- Namespace: `{meta.get('namespace', 'n/a')}`\n\n"
                f"Question: {query[:500]}\n\n"
                "_Set PROMETHEUS_URL to enable live PromQL queries._"
            )

        import httpx

        # Minimal live check
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(f"{self.prom_url}/api/v1/status/config")
            resp.raise_for_status()
        return (
            f"**Prometheus** (`{self.prom_url}`) is reachable.\n\n"
            f"Query received: {query[:800]}\n\n"
            "_Full PromQL builder can be added next; use kubernetes-agent for pod-level crashloop._"
        )


def main() -> None:
    host = os.environ.get("A2A_HOST", "0.0.0.0")
    port = int(os.environ.get("A2A_PORT", str(DEFAULT_PORT)))
    PrometheusAgent(host=host, port=port).run()


if __name__ == "__main__":
    main()
