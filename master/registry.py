"""A2A Agent Card discovery and registry (README master agent)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from common.config import agent_urls
from common.models import AgentCard

logger = logging.getLogger(__name__)

# Re-attempt to register previously-failed agents every N seconds
_RETRY_INTERVAL_S = 30.0


@dataclass
class RegisteredAgent:
    name: str
    url: str
    card: AgentCard

    @property
    def tags(self) -> set[str]:
        out: set[str] = set()
        for skill in self.card.skills:
            out.update(t.lower() for t in skill.tags)
        return out

    @property
    def examples(self) -> list[str]:
        examples: list[str] = []
        for skill in self.card.skills:
            examples.extend(skill.examples)
        return examples


class AgentRegistry:
    """Loads Agent Cards from /.well-known/agent.json for each configured URL."""

    def __init__(self, urls: Optional[list[str]] = None, timeout: float = 10.0):
        self._urls = urls if urls is not None else agent_urls()
        self._timeout = timeout
        self._agents: dict[str, RegisteredAgent] = {}
        # Track URLs that failed on the last attempt so we can retry them
        self._failed_urls: set[str] = set()
        self._last_retry_at: float = 0.0

    def refresh(self) -> dict[str, RegisteredAgent]:
        self._agents.clear()
        self._failed_urls.clear()
        for base in self._urls:
            try:
                agent = self._fetch_card(base)
                self._agents[agent.name] = agent
                logger.info("Registered agent %s at %s", agent.name, agent.url)
            except Exception as exc:
                logger.warning("Failed to load agent card from %s: %s", base, exc)
                self._failed_urls.add(base)
        return self._agents

    def _retry_failed(self) -> None:
        """Attempt to register any agent URLs that failed previously."""
        if not self._failed_urls:
            return
        recovered: set[str] = set()
        for base in list(self._failed_urls):
            try:
                agent = self._fetch_card(base)
                self._agents[agent.name] = agent
                recovered.add(base)
                logger.info("Recovered agent %s at %s", agent.name, agent.url)
            except Exception:
                pass
        self._failed_urls -= recovered
        self._last_retry_at = time.monotonic()

    def _fetch_card(self, base_url: str) -> RegisteredAgent:
        url = base_url.rstrip("/")
        with httpx.Client(timeout=self._timeout) as client:
            health = client.get(f"{url}/health")
            health.raise_for_status()
            resp = client.get(f"{url}/.well-known/agent.json")
            resp.raise_for_status()
            card = AgentCard(**resp.json())
        return RegisteredAgent(name=card.name, url=url, card=card)

    @property
    def agents(self) -> dict[str, RegisteredAgent]:
        if not self._agents:
            self.refresh()
        elif self._failed_urls and (time.monotonic() - self._last_retry_at) >= _RETRY_INTERVAL_S:
            self._retry_failed()
        return self._agents

    def get(self, name: str) -> Optional[RegisteredAgent]:
        return self.agents.get(name)

    def list_names(self) -> list[str]:
        return sorted(self.agents.keys())
