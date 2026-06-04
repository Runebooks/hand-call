"""Resolve Haystack / Prometheus org (X-Scope-OrgID) from alert metadata."""

from __future__ import annotations

import os
import re
from typing import Any

# Trigmetry may send Haystack tenant as hostname (e.g. fw-noc) instead of app-92387838
_HAYSTACK_TENANT_RE = re.compile(
    r"^(?:fw-noc(?:-[a-z0-9-]+)?|[a-z0-9-]+-fw-noc(?:-[a-z0-9-]+)?)$",
    re.I,
)
_FRESHDESK_APP_RE = re.compile(r"^app-\d+$", re.I)


def is_freshdesk_app_id(value: str) -> bool:
    return bool(_FRESHDESK_APP_RE.match((value or "").strip()))


def is_haystack_tenant(value: str) -> bool:
    """True when hostname (or similar field) is a Haystack tenant id, not an app/pod id."""
    token = (value or "").strip()
    if not token or is_freshdesk_app_id(token):
        return False
    return bool(_HAYSTACK_TENANT_RE.match(token))


def resolve_haystack_tenant(meta: dict[str, Any]) -> str:
    """
    Prometheus org header value (X-Scope-OrgID).

    Priority: explicit metadata → hostname if tenant-shaped → PROMETHEUS_ORG_ID env → fw-noc.
    """
    for key in ("haystack_tenant", "prometheus_org", "prometheus_org_id", "tenant"):
        val = (meta.get(key) or "").strip()
        if val:
            return val

    hostname = (meta.get("hostname") or "").strip()
    if hostname and is_haystack_tenant(hostname):
        return hostname

    env_org = os.environ.get("PROMETHEUS_ORG_ID", "").strip()
    return env_org or "fw-noc"
