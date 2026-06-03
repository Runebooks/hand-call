"""Serialize AlertContext into A2A task metadata (Slack → master-agent → specialists)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from master.alert_parser import AlertContext

_ALERT_FIELDS = (
    "raw_text",
    "subject",
    "alertname",
    "namespace",
    "cluster",
    "status",
    "severity",
    "priority",
    "region",
    "product",
    "hostname",
    "dashboard",
    "notification_channel",
    "alert_sre_attributes",
    "oncall",
    "fields",
)


def alert_to_metadata(alert: AlertContext) -> dict[str, Any]:
    data = asdict(alert)
    return {key: data[key] for key in _ALERT_FIELDS if data.get(key)}


def non_alert_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in meta.items() if k not in _ALERT_FIELDS}


def alert_from_metadata(meta: dict[str, Any]) -> Optional[AlertContext]:
    alertname = (meta.get("alertname") or "").strip()
    if not alertname:
        return None
    payload = {field: meta.get(field, "" if field != "fields" else {}) for field in _ALERT_FIELDS}
    if not isinstance(payload.get("fields"), dict):
        payload["fields"] = {}
    return AlertContext(**payload)
