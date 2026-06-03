"""A2A client for master-agent (Slack / webhook → master-agent → specialists)."""

from __future__ import annotations

import os
from typing import Any, Optional

from common.a2a_client import A2AClient
from common.models import AgentCapabilities, AgentCard, AgentSkill
from master.alert_metadata import alert_to_metadata
from master.alert_parser import AlertContext
from master.orchestrator import InvestigationResult, MasterOrchestrator
from master.registry import RegisteredAgent


class MasterAgentClient:
    """
    Calls master-agent via A2A tasks/send when MASTER_AGENT_URL is set.
    Falls back to in-process orchestrator when URL is empty (local dev only).
    """

    def __init__(self, base_url: Optional[str] = None, timeout: float = 120.0):
        if base_url is not None:
            self.base_url = base_url.strip().rstrip("/")
        else:
            self.base_url = os.environ.get("MASTER_AGENT_URL", "").strip().rstrip("/")
        self.timeout = timeout
        self._local: Optional[MasterOrchestrator] = None
        self._a2a: Optional[A2AClient] = None

    def _use_http(self) -> bool:
        return bool(self.base_url)

    def _local_orchestrator(self) -> MasterOrchestrator:
        if self._local is None:
            self._local = MasterOrchestrator()
        return self._local

    def _a2a_client(self) -> A2AClient:
        if self._a2a is None:
            self._a2a = A2AClient(base_url=self.base_url, timeout=self.timeout)
        return self._a2a

    def list_agents(self) -> list[str]:
        if not self._use_http():
            return self._local_orchestrator().list_agents()
        import httpx

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(f"{self.base_url}/agents")
            resp.raise_for_status()
            return resp.json().get("agents", [])

    def investigate(
        self,
        query: str,
        *,
        alert: Optional[AlertContext] = None,
        session_id: Optional[str] = None,
        extra_metadata: Optional[dict[str, Any]] = None,
    ) -> InvestigationResult:
        if not self._use_http():
            return self._local_orchestrator().investigate(
                query,
                alert=alert,
                session_id=session_id,
                extra_metadata=extra_metadata,
            )

        metadata: dict[str, Any] = dict(extra_metadata or {})
        if alert:
            metadata.update(alert_to_metadata(alert))

        task = self._a2a_client().send_task(
            query,
            session_id=session_id,
            metadata=metadata,
        )
        answer = self._a2a_client().get_answer_text(task)
        meta = task.metadata or {}

        agent_name = meta.get("routed_agent", "unknown")
        agent_url = meta.get("routed_agent_url", "")
        card_desc = meta.get("routed_agent_description", "")
        stub_card = AgentCard(
            name=agent_name,
            description=card_desc or agent_name,
            url=agent_url or "",
            skills=[AgentSkill(id="default", name="default", description="", tags=[])],
            capabilities=AgentCapabilities(),
        )
        registered = RegisteredAgent(name=agent_name, url=agent_url, card=stub_card)
        return InvestigationResult(
            agent=registered,
            answer=answer,
            query=query,
        )

    def format_reply(
        self,
        alert: AlertContext,
        result: InvestigationResult,
        *,
        user_prompt: str = "",
    ) -> str:
        orchestrator = (
            self._local_orchestrator()
            if not self._use_http()
            else MasterOrchestrator()
        )
        return orchestrator.format_reply(alert, result, user_prompt=user_prompt)
