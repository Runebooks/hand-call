"""Master orchestrator — route to specialist agents via A2A (no Temporal)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from common.a2a_client import A2AClient
from common.haystack_tenant import is_haystack_tenant, resolve_haystack_tenant
from master.alert_parser import AlertContext, format_slack_reply
from master.registry import AgentRegistry, RegisteredAgent
from master.router import AgentRouter

logger = logging.getLogger(__name__)


@dataclass
class InvestigationResult:
    agent: RegisteredAgent
    answer: str
    query: str


class MasterOrchestrator:
    """
    README flow (without Temporal):
      Slack → MasterOrchestrator → Router → specialist A2A agent → answer
    """

    def __init__(self, registry: Optional[AgentRegistry] = None):
        self.registry = registry or AgentRegistry()
        self.router = AgentRouter(self.registry)
        self._clients: dict[str, A2AClient] = {}

    def _client_for(self, agent: RegisteredAgent) -> A2AClient:
        if agent.name not in self._clients:
            self._clients[agent.name] = A2AClient(base_url=agent.url)
        return self._clients[agent.name]

    def list_agents(self) -> list[str]:
        return self.registry.list_names()

    def investigate(
        self,
        query: str,
        *,
        alert: Optional[AlertContext] = None,
        session_id: Optional[str] = None,
        extra_metadata: Optional[dict[str, Any]] = None,
    ) -> InvestigationResult:
        agent = self.router.route(query, alert)
        metadata: dict[str, Any] = {"source": "master", "routed_agent": agent.name}
        if alert:
            pod = alert.pod_hint or alert.alert_sre_attributes or ""
            if not pod and alert.hostname and not is_haystack_tenant(alert.hostname):
                pod = alert.hostname
            sre = alert.alert_sre_attributes or ""
            if not sre and alert.hostname and not is_haystack_tenant(alert.hostname):
                sre = alert.hostname
            metadata.update(
                {
                    "alertname": alert.alertname,
                    "namespace": alert.namespace,
                    "cluster": alert.cluster,
                    "pod": pod,
                    "alert_sre_attributes": sre,
                    "hostname": alert.hostname,
                    "product": alert.product,
                    "current_value": alert.fields.get("current_value", ""),
                    "threshold": alert.fields.get("threshold", ""),
                    "summary": alert.summary or alert.fields.get("summary", ""),
                    "dashboard": alert.dashboard,
                    "severity": alert.severity,
                    "priority": alert.priority,
                    "region": alert.region,
                }
            )
            metadata["haystack_tenant"] = resolve_haystack_tenant(metadata)
        if extra_metadata:
            metadata.update(extra_metadata)

        client = self._client_for(agent)
        logger.info("Master routing → %s (%s)", agent.name, agent.url)
        task = client.send_task(query, session_id=session_id, metadata=metadata)
        answer = client.get_answer_text(task)
        return InvestigationResult(agent=agent, answer=answer, query=query)

    def format_reply(
        self,
        alert: AlertContext,
        result: InvestigationResult,
        *,
        user_prompt: str = "",
    ) -> str:
        header = f"_via *{result.agent.name}_*"
        answer = result.answer or "_No details returned._"
        if (user_prompt or "").strip():
            return f"{header}\n{answer}"
        body = format_slack_reply(alert, answer)
        return f"{header}\n{body}"
