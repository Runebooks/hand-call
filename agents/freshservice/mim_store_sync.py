"""
6-month MIM store sync — populates the fw-noc PostgreSQL MIM_Store cache.

Pulls the last ~6 months of Major Incident (MIM) tickets from Freshservice, assembles
each ticket's full PIR (metrics + timeline + personnel), and upserts into MIM_Store.
The agent uses this table FALLBACK-ONLY when the live Freshservice API rate-limits.

Run as a K8s CronJob (e.g. every 6h) or directly:
  python -m agents.freshservice.mim_store_sync

Requires FRESHSERVICE_API_KEY and PG* connection env. Skips gracefully if either
is unconfigured.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DEFAULT_MONTHS = 6
_DEFAULT_MAX = 400


def run_once() -> None:
    from agents.freshservice.freshservice_client import (
        FreshserviceClient,
        FreshserviceUnavailable,
    )
    from agents.freshservice.pg_store import PgStore

    months = int(os.environ.get("MIM_STORE_MONTHS", str(_DEFAULT_MONTHS)))
    max_tickets = int(os.environ.get("MIM_STORE_MAX", str(_DEFAULT_MAX)))

    fs = FreshserviceClient()
    if not fs.enabled:
        logger.warning("FRESHSERVICE_API_KEY not set; skipping MIM store sync.")
        return

    store = PgStore()
    if not store.enabled:
        logger.warning("PGHOST not set; skipping MIM store sync (Postgres required).")
        return

    logger.info("Ensuring MIM_Store schema…")
    store.ensure_schema()

    logger.info("Listing Major Incidents for the last %d months (max %d)…", months, max_tickets)
    try:
        tickets = fs.list_major_incidents(months_back=months, limit=max_tickets)
    except FreshserviceUnavailable as exc:
        logger.error("Freshservice unavailable during list (%s); aborting this run.", exc)
        return

    logger.info("Found %d MIM tickets; assembling PIRs…", len(tickets))
    upserted = 0
    failed = 0
    for t in tickets:
        tid = t.get("id")
        if tid is None:
            continue
        try:
            pir = fs.get_pir(tid)
        except FreshserviceUnavailable as exc:
            # Rate-limited mid-run: back off briefly and retry once, else stop.
            logger.warning("Rate-limited on ticket %s (%s); backing off 30s…", tid, exc)
            time.sleep(30)
            try:
                pir = fs.get_pir(tid)
            except FreshserviceUnavailable:
                logger.error("Still rate-limited; stopping run early with %d upserted.", upserted)
                break
        if not pir or "error" in pir:
            failed += 1
            continue
        if store.upsert_pir(pir):
            upserted += 1
        else:
            failed += 1
        # Gentle pacing to stay under Freshservice rate limits.
        time.sleep(0.4)

    logger.info("MIM store sync done: upserted=%d failed=%d total_in_store=%d",
                upserted, failed, store.count())


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_once()


if __name__ == "__main__":
    main()
