"""
A2A protocol client — send tasks to agent servers and read artifacts.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from .models import JSONRPCResponse, Message, Task, TaskSendParams


class A2AClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 120.0):
        self.base_url = (
            base_url
            or os.environ.get(
                "KUBERNETES_AGENT_URL",
                "http://kubernetes-agent.a2a-ops.svc.cluster.local:8082",
            )
        ).rstrip("/")
        self.timeout = timeout

    def send_task(
        self,
        text: str,
        session_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Task:
        params = TaskSendParams(
            message=Message.user(text),
            session_id=session_id,
            metadata=metadata or {},
        )
        body = {
            "jsonrpc": "2.0",
            "id": "slack-bot",
            "method": "tasks/send",
            "params": params.model_dump(mode="json"),
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}/", json=body)
            response.raise_for_status()
            rpc = JSONRPCResponse(**response.json())

        if rpc.error:
            raise RuntimeError(f"A2A error {rpc.error.code}: {rpc.error.message}")
        if rpc.result is None:
            raise RuntimeError("A2A returned empty result")
        return Task(**rpc.result)

    def get_answer_text(self, task: Task) -> str:
        parts = []
        for artifact in task.artifacts:
            parts.append(artifact.get_text())
        return "\n\n".join(p for p in parts if p).strip()
