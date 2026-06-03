"""Shared configuration from environment."""

from __future__ import annotations

import os
from typing import List


def agent_urls() -> List[str]:
    """
    Comma-separated A2A agent base URLs (README: master discovers all agents).

    Example:
      http://prometheus-agent.a2a-ops.svc.cluster.local:8080,
      http://rds-agent.a2a-ops.svc.cluster.local:8081,
      http://kubernetes-agent.a2a-ops.svc.cluster.local:8082
    """
    raw = os.environ.get("AGENT_URLS", "").strip()
    if raw:
        return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
    # Backward compatible default: K8s only
    k8s = os.environ.get(
        "KUBERNETES_AGENT_URL",
        "http://kubernetes-agent.a2a-ops.svc.cluster.local:8082",
    ).strip()
    return [k8s.rstrip("/")] if k8s else []


def master_agent_url() -> str:
    """
    Master router pod URL. Slack/n8n call this; master calls AGENT_URLS specialists.

    Unset = slack runs router in-process (local dev). In cluster set:
      http://master-agent.a2a-ops.svc.cluster.local:8095
    """
    return os.environ.get("MASTER_AGENT_URL", "").strip().rstrip("/")
