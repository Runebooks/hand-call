"""Serialize AlertContext into A2A task metadata (Slack → master-agent → specialists)."""

from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any, Optional

from master.alert_parser import AlertContext

# Fields on AlertContext dataclass only
_CONTEXT_FIELDS = frozenset(f.name for f in fields(AlertContext))

# Extra NOC/Trigmetry keys stored in alert.fields and flattened in task metadata
_EXTRA_FIELD_KEYS = frozenset(
    {
        "current_value",
        "threshold",
        "alert_type",
        "source",
        "service",
        "summary",
        "dashboard1",
    }
)

# All keys owned by alert serialization (exclude from extra_metadata passthrough)
_ALERT_METADATA_KEYS = _CONTEXT_FIELDS | _EXTRA_FIELD_KEYS | frozenset({"pod", "hostname"})


def alert_to_metadata(alert: AlertContext) -> dict[str, Any]:
    """Flat metadata for A2A tasks (specialists read top-level keys)."""
    data = asdict(alert)
    out: dict[str, Any] = {}
    for key in _CONTEXT_FIELDS:
        val = data.get(key)
        if val:
            out[key] = val
    for key in _EXTRA_FIELD_KEYS:
        val = alert.fields.get(key)
        if val:
            out[key] = val
    return out


def non_alert_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in meta.items() if k not in _ALERT_METADATA_KEYS}


def alert_from_metadata(meta: dict[str, Any]) -> Optional[AlertContext]:
    """Rebuild AlertContext from task metadata without invalid constructor kwargs."""
    alertname = (meta.get("alertname") or "").strip()
    if not alertname:
        return None

    payload: dict[str, Any] = {}
    for key in _CONTEXT_FIELDS:
        if key == "fields":
            continue
        default: Any = "" if key != "fields" else {}
        val = meta.get(key, default)
        if val is not None and val != "":
            payload[key] = val

    merged_fields: dict[str, str] = {}
    raw_fields = meta.get("fields")
    if isinstance(raw_fields, dict):
        merged_fields.update({k: str(v) for k, v in raw_fields.items() if v})

    for key in _EXTRA_FIELD_KEYS:
        val = meta.get(key)
        if val:
            merged_fields[key] = str(val)

    payload["fields"] = merged_fields
    payload.setdefault("raw_text", "")
    payload.setdefault("alertname", alertname)
    return AlertContext(**payload)
