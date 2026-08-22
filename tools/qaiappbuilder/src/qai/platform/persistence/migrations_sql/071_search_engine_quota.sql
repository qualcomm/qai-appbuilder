-- =====================================================================
-- 071_search_engine_quota.sql
--
-- Per-engine monthly usage quota for keyed (paid) search engines.
-- Tracks the number of search invocations per engine per calendar month,
-- allowing the application to enforce configurable monthly limits and
-- emit threshold warnings (e.g. every 100 uses, then every 20 after 900).
--
-- One row per (engine_id, month_key) pair.  ``month_key`` is ISO
-- ``YYYY-MM`` so querying the current month is a simple equality check.
-- The ``monthly_limit`` column stores the per-engine cap (default 1000);
-- it lives here (denormalized from config) so a single atomic
-- UPDATE + SELECT can check + increment without a second config lookup.
--
-- ``notify_enabled`` controls whether the frontend should show quota
-- threshold warnings for this engine.  Users can disable per-engine.
-- =====================================================================

CREATE TABLE IF NOT EXISTS search_engine_quota (
    engine_id       TEXT    NOT NULL,
    month_key       TEXT    NOT NULL,  -- 'YYYY-MM'
    usage_count     INTEGER NOT NULL DEFAULT 0,
    monthly_limit   INTEGER NOT NULL DEFAULT 1000,
    notify_enabled  INTEGER NOT NULL DEFAULT 1,  -- boolean: 1=on, 0=off
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (engine_id, month_key)
);

-- Index for fast "all engines this month" listing (the settings panel).
CREATE INDEX IF NOT EXISTS idx_seq_month
    ON search_engine_quota (month_key);
