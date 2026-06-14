"""
PostgreSQL-backed MIM store (fw-noc RDS) — fallback cache for the Freshservice agent.

Holds a single self-bootstrapping table ``MIM_Store`` with the last ~6 months of
Major Incident tickets + assembled PIR data. This is used FALLBACK-ONLY: the agent
reads it when the live Freshservice API rate-limits (429) or is unavailable (5xx /
connect error).

Connection via standard libpq env vars (overridable):
  PGHOST, PGPORT (5432), PGDATABASE (fw-noc), PGUSER (dbuser), PGPASSWORD, PGSSLMODE (require)

Gracefully disabled when PGHOST is unset — all methods become no-ops / return [].
Requires: psycopg2-binary
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "MIM_Store" (
    ticket_id            BIGINT PRIMARY KEY,
    subject              TEXT,
    status               INTEGER,
    priority             INTEGER,
    product              TEXT,
    module               TEXT,
    products_affected    JSONB,
    regions_affected     JSONB,
    issue_category       TEXT,
    major_incident_type  TEXT,
    type_of_incident     TEXT,
    incident_start_time  TEXT,
    incident_end_time    TEXT,
    mtta_minutes         INTEGER,
    mttd_minutes         INTEGER,
    mttr_minutes         INTEGER,
    impact_to_customer   TEXT,
    statuspage_url       TEXT,
    pir_status           TEXT,
    pir_number           TEXT,
    pir_url              TEXT,
    personnel            JSONB,
    timeline_events      JSONB,
    raw_pir              JSONB,
    created_at           TEXT,
    synced_at            TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mim_store_issue_category_idx ON "MIM_Store" (issue_category);
CREATE INDEX IF NOT EXISTS mim_store_start_idx ON "MIM_Store" (incident_start_time);
CREATE INDEX IF NOT EXISTS mim_store_product_idx ON "MIM_Store" (product);
"""

_UPSERT_SQL = """
INSERT INTO "MIM_Store" (
    ticket_id, subject, status, priority, product, module,
    products_affected, regions_affected, issue_category, major_incident_type,
    type_of_incident, incident_start_time, incident_end_time,
    mtta_minutes, mttd_minutes, mttr_minutes, impact_to_customer,
    statuspage_url, pir_status, pir_number, pir_url,
    personnel, timeline_events, raw_pir, created_at, synced_at
) VALUES (
    %(ticket_id)s, %(subject)s, %(status)s, %(priority)s, %(product)s, %(module)s,
    %(products_affected)s, %(regions_affected)s, %(issue_category)s, %(major_incident_type)s,
    %(type_of_incident)s, %(incident_start_time)s, %(incident_end_time)s,
    %(mtta_minutes)s, %(mttd_minutes)s, %(mttr_minutes)s, %(impact_to_customer)s,
    %(statuspage_url)s, %(pir_status)s, %(pir_number)s, %(pir_url)s,
    %(personnel)s, %(timeline_events)s, %(raw_pir)s, %(created_at)s, now()
)
ON CONFLICT (ticket_id) DO UPDATE SET
    subject = EXCLUDED.subject,
    status = EXCLUDED.status,
    priority = EXCLUDED.priority,
    product = EXCLUDED.product,
    module = EXCLUDED.module,
    products_affected = EXCLUDED.products_affected,
    regions_affected = EXCLUDED.regions_affected,
    issue_category = EXCLUDED.issue_category,
    major_incident_type = EXCLUDED.major_incident_type,
    type_of_incident = EXCLUDED.type_of_incident,
    incident_start_time = EXCLUDED.incident_start_time,
    incident_end_time = EXCLUDED.incident_end_time,
    mtta_minutes = EXCLUDED.mtta_minutes,
    mttd_minutes = EXCLUDED.mttd_minutes,
    mttr_minutes = EXCLUDED.mttr_minutes,
    impact_to_customer = EXCLUDED.impact_to_customer,
    statuspage_url = EXCLUDED.statuspage_url,
    pir_status = EXCLUDED.pir_status,
    pir_number = EXCLUDED.pir_number,
    pir_url = EXCLUDED.pir_url,
    personnel = EXCLUDED.personnel,
    timeline_events = EXCLUDED.timeline_events,
    raw_pir = EXCLUDED.raw_pir,
    created_at = EXCLUDED.created_at,
    synced_at = now()
"""


def _to_int(val: Any) -> Optional[int]:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


class PgStore:
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        database: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        sslmode: Optional[str] = None,
    ):
        self.host = (host or os.environ.get("PGHOST", "")).strip()
        self.port = int(port or os.environ.get("PGPORT", "5432") or 5432)
        self.database = (database or os.environ.get("PGDATABASE", "fw-noc")).strip()
        self.user = (user or os.environ.get("PGUSER", "dbuser")).strip()
        self._password = (password or os.environ.get("PGPASSWORD", "")).strip()
        self.sslmode = (sslmode or os.environ.get("PGSSLMODE", "require")).strip()
        self._conn: Any = None

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    def _connect(self) -> Any:
        if self._conn is not None:
            try:
                if self._conn.closed == 0:
                    return self._conn
            except Exception:
                pass
            self._conn = None
        try:
            import psycopg2
            import psycopg2.extras  # noqa: F401

            self._conn = psycopg2.connect(
                host=self.host,
                port=self.port,
                dbname=self.database,
                user=self.user,
                password=self._password,
                sslmode=self.sslmode,
                connect_timeout=10,
            )
            return self._conn
        except ImportError:
            raise RuntimeError("psycopg2 not installed; add psycopg2-binary>=2.9 to requirements.txt")
        except Exception as exc:
            logger.warning("Postgres connect failed: %s", exc)
            raise

    def ensure_schema(self) -> None:
        """Create the MIM_Store table + indexes if missing."""
        if not self.enabled:
            return
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()

    def _dict_cursor(self, conn: Any):
        import psycopg2.extras

        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── Writes (sync job) ────────────────────────────────────────────────────

    def upsert_pir(self, pir: dict[str, Any]) -> bool:
        """Upsert one assembled PIR/ticket dict into MIM_Store. Returns success."""
        if not self.enabled:
            return False
        ticket_id = _to_int(pir.get("ticket_id"))
        if ticket_id is None:
            return False
        row = {
            "ticket_id": ticket_id,
            "subject": pir.get("subject"),
            "status": _to_int(pir.get("status")),
            "priority": _to_int(pir.get("priority")),
            "product": pir.get("product"),
            "module": pir.get("module"),
            "products_affected": json.dumps(pir.get("products_affected") or []),
            "regions_affected": json.dumps(pir.get("regions_affected") or []),
            "issue_category": pir.get("issue_category"),
            "major_incident_type": pir.get("major_incident_type"),
            "type_of_incident": pir.get("type_of_incident"),
            "incident_start_time": pir.get("incident_start_time"),
            "incident_end_time": pir.get("incident_end_time"),
            "mtta_minutes": _to_int(pir.get("mtta_minutes")),
            "mttd_minutes": _to_int(pir.get("mttd_minutes")),
            "mttr_minutes": _to_int(pir.get("mttr_minutes")),
            "impact_to_customer": pir.get("impact_to_customer"),
            "statuspage_url": pir.get("statuspage_url"),
            "pir_status": pir.get("pir_status"),
            "pir_number": pir.get("pir_number"),
            "pir_url": pir.get("pir_url"),
            "personnel": json.dumps(pir.get("personnel") or []),
            "timeline_events": json.dumps(pir.get("timeline_events") or []),
            "raw_pir": json.dumps(pir, default=str),
            "created_at": pir.get("created_at"),
        }
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(_UPSERT_SQL, row)
            conn.commit()
            return True
        except Exception as exc:
            logger.warning("MIM_Store upsert failed for ticket %s: %s", ticket_id, exc)
            try:
                self._connect().rollback()
            except Exception:
                pass
            return False

    # ── Reads (fallback path) ─────────────────────────────────────────────────

    def _query(self, sql: str, args: tuple = ()) -> list[dict]:
        if not self.enabled:
            return []
        try:
            conn = self._connect()
            with self._dict_cursor(conn) as cur:
                cur.execute(sql, args)
                return [dict(r) for r in (cur.fetchall() or [])]
        except Exception as exc:
            logger.warning("MIM_Store query failed: %s", exc)
            return []

    def store_get_pir(self, ticket_id: int | str) -> dict[str, Any]:
        """Return the cached assembled PIR (raw_pir) for one ticket, or {}."""
        tid = _to_int(ticket_id)
        if tid is None:
            return {}
        rows = self._query('SELECT raw_pir FROM "MIM_Store" WHERE ticket_id = %s', (tid,))
        if not rows:
            return {}
        raw = rows[0].get("raw_pir")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return raw or {}

    def store_get_ticket(self, ticket_id: int | str) -> dict[str, Any]:
        """Return the cached slim ticket row for one ticket, or {}."""
        tid = _to_int(ticket_id)
        if tid is None:
            return {}
        rows = self._query('SELECT * FROM "MIM_Store" WHERE ticket_id = %s', (tid,))
        return rows[0] if rows else {}

    def store_list(
        self,
        issue_category: str = "",
        product: str = "",
        months_back: int = 0,
        limit: int = 30,
    ) -> list[dict]:
        """List cached MIM rows with optional filters, newest-first."""
        clauses: list[str] = []
        args: list[Any] = []
        if issue_category:
            clauses.append("LOWER(issue_category) LIKE %s")
            args.append(f"%{issue_category.lower()}%")
        if product:
            clauses.append("(LOWER(product) LIKE %s OR LOWER(subject) LIKE %s OR products_affected::text ILIKE %s)")
            args.extend([f"%{product.lower()}%", f"%{product.lower()}%", f"%{product.lower()}%"])
        if months_back and months_back > 0:
            from datetime import datetime, timedelta, timezone

            cutoff = (datetime.now(timezone.utc) - timedelta(days=30 * months_back)).strftime("%Y-%m-%d")
            clauses.append("LEFT(incident_start_time, 10) >= %s")
            args.append(cutoff)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            'SELECT ticket_id AS id, subject, status, priority, product, module, '
            'products_affected, regions_affected, issue_category, major_incident_type, '
            'type_of_incident, mtta_minutes, mttd_minutes, mttr_minutes, '
            'incident_start_time, incident_end_time, impact_to_customer, '
            'statuspage_url, pir_status, pir_number, pir_url '
            'FROM "MIM_Store"' + where +
            " ORDER BY incident_start_time DESC NULLS LAST LIMIT %s"
        )
        args.append(int(limit))
        return self._query(sql, tuple(args))

    def count(self) -> int:
        rows = self._query('SELECT COUNT(*) AS n FROM "MIM_Store"')
        return int(rows[0]["n"]) if rows else 0
