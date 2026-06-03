"""In-memory pending mutation confirmations keyed by A2A session (Slack thread)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class PendingMutation:
    operation: str
    namespace: str
    pod: Optional[str] = None
    deployment: Optional[str] = None
    scale_replicas: Optional[int] = None
    summary: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.time()


class PendingMutationStore:
    def __init__(self, ttl_seconds: float = 600.0):
        self._ttl = ttl_seconds
        self._pending: dict[str, PendingMutation] = {}

    def get(self, session_id: str) -> Optional[PendingMutation]:
        if not session_id:
            return None
        pending = self._pending.get(session_id)
        if pending is None:
            return None
        if time.time() - pending.created_at > self._ttl:
            del self._pending[session_id]
            return None
        return pending

    def set(self, session_id: str, pending: PendingMutation) -> None:
        if not session_id:
            return
        self._pending[session_id] = pending

    def clear(self, session_id: str) -> None:
        if session_id in self._pending:
            del self._pending[session_id]


PENDING_STORE = PendingMutationStore()
