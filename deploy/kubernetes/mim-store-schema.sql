-- MIM_Store: 6-month Major Incident cache for the freshservice-agent (fw-noc PostgreSQL RDS).
-- Used FALLBACK-ONLY when the live Freshservice API rate-limits (429) or is unavailable.
-- The agent also creates this automatically (CREATE TABLE IF NOT EXISTS) on first sync;
-- this file is provided for manual provisioning / review.
--
-- Connect (example):
--   psql "host=fw-noc.clyoag7kp3ki.us-east-1.rds.amazonaws.com port=5432 dbname=fw-noc user=dbuser sslmode=require"

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
