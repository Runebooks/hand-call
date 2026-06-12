"""
Hourly Freshservice Analytics export → MySQL upsert.

Ported from n8n Code: Parse analytics CSV (scheduled hourly).
Run as a K8s CronJob or directly:
  python -m agents.freshservice.analytics_sync

Requires MySQL credentials (MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE)
and Freshservice API credentials. Skip gracefully when MySQL is unconfigured.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DEFAULT_EXPORT_ID = "1fe8fb0f-bc52-4377-813e-54087dd43429"

# ── Cause classification (ported from n8n classifyIncidentRow) ────────────────

_CAUSE_KEYWORDS: dict[str, list[str]] = {
    "deployment": ["deploy", "release", "rollout", "upgrade", "migration"],
    "infra": ["infra", "infrastructure", "hardware", "server", "disk", "network", "aws", "cloud"],
    "database": ["database", "db", "mysql", "postgres", "sql", "rds", "redis"],
    "third-party": ["third party", "third-party", "vendor", "external", "aws service"],
    "configuration": ["config", "configuration", "misconfiguration", "setting"],
    "code": ["code", "bug", "defect", "regression"],
    "unknown": [],
}

_ISSUE_CATEGORY_MAP: dict[str, str] = {
    "code": "Code",
    "infra": "Infra",
    "configuration": "Configuration",
    "third-party": "Third-party",
    "database": "Database",
    "deployment": "Deployment",
}

_STATUS_PAGE_KEYWORDS = ["status page", "statuspage", "freshstatus", "public update"]
_MOM_KEYWORDS = ["mom", "meeting of minds", "post-incident", "presented"]


def _classify(subject: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Classify a ticket row into cause_segment, issue_category, product flags."""
    blob = " ".join([
        str(subject or ""),
        str(raw.get("custom_fields", {}) if isinstance(raw.get("custom_fields"), dict) else ""),
        str(raw.get("issue_category") or ""),
        str(raw.get("module") or ""),
    ]).lower()

    cause = "unknown"
    for seg, keywords in _CAUSE_KEYWORDS.items():
        if any(kw in blob for kw in keywords):
            cause = seg
            break

    issue_category = _ISSUE_CATEGORY_MAP.get(cause, "Other")
    status_page_flag = any(kw in blob for kw in _STATUS_PAGE_KEYWORDS)
    mom_flag = any(kw in blob for kw in _MOM_KEYWORDS)

    return {
        "cause_segment": cause,
        "issue_category": issue_category,
        "third_party": (cause == "third-party"),
        "status_page_flag": status_page_flag,
        "mom_presented_flag": mom_flag,
    }


def _parse_csv(text: str) -> list[dict[str, str]]:
    """Parse CSV text into a list of row dicts."""
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _normalize_row(raw: dict[str, str]) -> Optional[dict[str, Any]]:
    """Normalize a CSV row into a MIM_Analytics_export schema dict."""
    from datetime import datetime

    ticket_id = (
        raw.get("ID") or raw.get("id") or raw.get("ticket_id") or ""
    ).strip().lstrip("#")
    if not ticket_id or not ticket_id.isdigit():
        return None

    subject = raw.get("Subject") or raw.get("subject") or raw.get("Summary") or ""
    status = raw.get("Status") or raw.get("status") or ""
    priority = raw.get("Priority") or raw.get("priority") or ""
    source = raw.get("Source") or raw.get("source") or ""
    created_date = raw.get("Created Date") or raw.get("Created") or raw.get("created_date") or ""
    workspace = raw.get("Workspace") or raw.get("workspace") or ""
    ticket_type = raw.get("Type") or raw.get("type") or raw.get("ticket_type") or ""
    product = raw.get("Product") or raw.get("product") or raw.get("Impacted Product") or ""

    classified = _classify(subject, raw)
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    return {
        "ticket_id": ticket_id,
        "ticket_display_id": raw.get("Ticket Display ID") or f"#{ticket_id}",
        "subject": subject or None,
        "status": status or None,
        "priority": priority or None,
        "source": source or None,
        "created_date": created_date or None,
        "workspace": workspace or None,
        "ticket_type": ticket_type or None,
        "product": product or None,
        "issue_category": classified["issue_category"],
        "cause_segment": classified["cause_segment"],
        "third_party": 1 if classified["third_party"] else 0,
        "status_page_flag": 1 if classified["status_page_flag"] else 0,
        "mom_presented_flag": 1 if classified["mom_presented_flag"] else 0,
        "export_synced_at": now_str,
        "raw_row": json.dumps(raw),
    }


_UPSERT_SQL = """
INSERT INTO `MIM_Analytics_export` (
  ticket_id, ticket_display_id, subject, status, priority, source,
  created_date, workspace, ticket_type, product, issue_category, cause_segment,
  third_party, status_page_flag, mom_presented_flag, export_synced_at, raw_row
) VALUES (
  %(ticket_id)s, %(ticket_display_id)s, %(subject)s, %(status)s, %(priority)s, %(source)s,
  %(created_date)s, %(workspace)s, %(ticket_type)s, %(product)s, %(issue_category)s,
  %(cause_segment)s, %(third_party)s, %(status_page_flag)s, %(mom_presented_flag)s,
  %(export_synced_at)s, %(raw_row)s
)
ON DUPLICATE KEY UPDATE
  ticket_display_id = VALUES(ticket_display_id),
  subject           = VALUES(subject),
  status            = VALUES(status),
  priority          = VALUES(priority),
  source            = VALUES(source),
  created_date      = VALUES(created_date),
  workspace         = VALUES(workspace),
  ticket_type       = VALUES(ticket_type),
  product           = COALESCE(VALUES(product), product),
  issue_category    = VALUES(issue_category),
  cause_segment     = VALUES(cause_segment),
  third_party       = VALUES(third_party),
  status_page_flag  = VALUES(status_page_flag),
  mom_presented_flag = VALUES(mom_presented_flag),
  export_synced_at  = VALUES(export_synced_at),
  raw_row           = VALUES(raw_row)
""".strip()


def run_once() -> None:
    from agents.freshservice.freshservice_client import FreshserviceClient
    from agents.freshservice.mysql_client import MySQLClient

    export_id = os.environ.get("FRESHSERVICE_ANALYTICS_EXPORT_ID", _DEFAULT_EXPORT_ID).strip()
    fs = FreshserviceClient()
    if not fs.enabled:
        logger.warning("FRESHSERVICE_API_KEY not set; skipping analytics sync.")
        return

    db = MySQLClient()
    if not db.enabled:
        logger.warning("MYSQL_HOST not set; skipping analytics sync (MySQL required for Phase 2).")
        return

    logger.info("Fetching Freshservice Analytics export id=%s", export_id)
    csv_text = fs.get_analytics_export_csv(export_id)
    if not csv_text.strip():
        logger.warning("Analytics export returned empty; check export id and Freshservice schedule.")
        return

    rows = _parse_csv(csv_text)
    logger.info("Parsed %d CSV rows", len(rows))

    normalized = [_normalize_row(r) for r in rows]
    normalized = [r for r in normalized if r is not None]
    logger.info("Valid rows to upsert: %d", len(normalized))
    if not normalized:
        return

    conn = db._connect()
    upserted = 0
    with conn.cursor() as cur:
        for row in normalized:
            try:
                cur.execute(_UPSERT_SQL, row)
                upserted += 1
            except Exception as exc:
                logger.warning("Upsert failed for ticket_id=%s: %s", row.get("ticket_id"), exc)
    conn.commit()
    logger.info("Upserted %d rows into MIM_Analytics_export", upserted)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_once()


if __name__ == "__main__":
    main()
