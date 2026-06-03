"""A2A Agent Card discovery and registry (README master agent)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from common.config import agent_urls
from common.models import AgentCard

logger = logging.getLogger(__name__)


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

    def refresh(self) -> dict[str, RegisteredAgent]:
        self._agents.clear()
        for base in self._urls:
            try:
                agent = self._fetch_card(base)
                self._agents[agent.name] = agent
                logger.info("Registered agent %s at %s", agent.name, agent.url)
            except Exception as exc:
                logger.warning("Failed to load agent card from %s: %s", base, exc)
        return self._agents

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
        return self._agents

    def get(self, name: str) -> Optional[RegisteredAgent]:
        return self.agents.get(name)

    def list_names(self) -> list[str]:
        return sorted(self.agents.keys())
