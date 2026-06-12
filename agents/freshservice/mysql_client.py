"""
Read-only MySQL client for the Freshworks Incident Automation database.

Tables used (same as n8n workflow):
  - Outages_data          — internal incident records
  - All_update_threads    — Slack thread update rows per incident
  - MIM_Ticket_id         — MIM ticket ↔ incident mappings
  - MIM_Analytics_export  — hourly Analytics CSV export upserted from Freshservice

Gracefully disabled when MYSQL_HOST is not set — all query methods return [].
Requires: pymysql  (add to requirements.txt: pymysql>=1.1.0)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


class MySQLClient:
    def __init__(
        self,
        host: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        port: int = 3306,
    ):
        self.host = (host or os.environ.get("MYSQL_HOST", "")).strip()
        self.user = (user or os.environ.get("MYSQL_USER", "")).strip()
        self._password = (password or os.environ.get("MYSQL_PASSWORD", "")).strip()
        self.database = (database or os.environ.get("MYSQL_DATABASE", "")).strip()
        self.port = int(os.environ.get("MYSQL_PORT", str(port)))
        self._conn: Any = None

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.user)

    def _connect(self) -> Any:
        if self._conn is not None:
            try:
                self._conn.ping(reconnect=True)
                return self._conn
            except Exception:
                self._conn = None
        try:
            import pymysql
            import pymysql.cursors

            self._conn = pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self._password,
                database=self.database,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=10,
                read_timeout=15,
            )
            return self._conn
        except ImportError:
            raise RuntimeError("pymysql not installed; add pymysql>=1.1.0 to requirements.txt")
        except Exception as exc:
            logger.warning("MySQL connect failed: %s", exc)
            raise

    def _query(self, sql: str, args: tuple = ()) -> list[dict]:
        if not self.enabled:
            return []
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchall() or []
        except Exception as exc:
            logger.warning("MySQL query failed: %s", exc)
            return []

    def query_outages(
        self,
        limit: int = 150,
        product: Optional[str] = None,
        incident_no: Optional[str] = None,
    ) -> list[dict]:
        """Fetch Outages_data rows, newest first by create_epoch_date_time."""
        sql = """
            SELECT 'Outages_data' AS _mysql_source, o.*
            FROM `Outages_data` AS o
            ORDER BY
              CASE
                WHEN CONCAT(IFNULL(o.create_epoch_date_time,'')) REGEXP '^[0-9]+(\\.[0-9]+)?$'
                  THEN CAST(o.create_epoch_date_time AS DECIMAL(24,6))
                WHEN NULLIF(TRIM(CAST(IFNULL(o.create_epoch_date_time,'') AS CHAR)),'') IS NOT NULL
                  AND TRIM(CAST(o.create_epoch_date_time AS CHAR)) REGEXP '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                  THEN UNIX_TIMESTAMP(STR_TO_DATE(
                    SUBSTRING(TRIM(CAST(o.create_epoch_date_time AS CHAR)),1,19),
                    '%%Y-%%m-%%d %%H:%%i:%%s'))
                ELSE 0
              END DESC,
              CAST(o.incident_no AS UNSIGNED) DESC
            LIMIT %s
        """
        rows = self._query(sql, (limit,))
        if incident_no:
            rows = [r for r in rows if str(r.get("incident_no") or "") == str(incident_no)]
        if product:
            p_lower = product.lower()
            rows = [
                r for r in rows
                if p_lower in str(r.get("Product") or r.get("product") or "").lower()
            ]
        return rows

    def query_mim_tickets(self, limit: int = 100) -> list[dict]:
        """Fetch MIM_Ticket_id rows (Ticket_field, Slack_thread, Product, Region, Priority)."""
        sql = """
            SELECT 'MIM_Ticket_id' AS _mysql_source, t.*
            FROM `MIM_Ticket_id` AS t
            LIMIT %s
        """
        return self._query(sql, (limit,))

    def query_update_threads(self, limit: int = 18, incident_no: Optional[str] = None) -> list[dict]:
        """Fetch All_update_threads rows for an incident."""
        sql = """
            SELECT 'All_update_threads' AS _mysql_source, t.*
            FROM `All_update_threads` AS t
            ORDER BY t.Incident_no DESC,
                     CAST(t.Slack_update_threads AS DECIMAL(20,6)) DESC
            LIMIT %s
        """
        rows = self._query(sql, (limit,))
        if incident_no:
            rows = [
                r for r in rows
                if str(r.get("Incident_no") or r.get("incident_no") or "") == str(incident_no)
            ]
        return rows

    def query_mim_analytics(
        self,
        limit: int = 500,
        product: Optional[str] = None,
    ) -> list[dict]:
        """Fetch MIM_Analytics_export rows, newest first."""
        sql = """
            SELECT 'MIM_Analytics_export' AS _mysql_source, e.*
            FROM `MIM_Analytics_export` AS e
            ORDER BY e.created_date DESC
            LIMIT %s
        """
        rows = self._query(sql, (limit,))
        if product:
            p_lower = product.lower()
            rows = [
                r for r in rows
                if p_lower in str(r.get("product") or r.get("Product") or "").lower()
            ]
        return rows

    def merge_all(
        self,
        max_outages: int = 18,
        max_threads: int = 12,
        max_mim: int = 12,
    ) -> list[dict]:
        """Merge and trim all source tables into one list for LLM context."""
        rows: list[dict] = []
        rows.extend(self.query_outages(limit=max_outages))
        rows.extend(self.query_update_threads(limit=max_threads))
        rows.extend(self.query_mim_tickets(limit=max_mim))
        return rows
