-- =====================================================================
-- 063_search_engine_score.sql
--
-- Per-engine health score for the independent web-search aggregator
-- (plan web-search-independent-engine-integration §12). One row per
-- engine, keyed by ``engine_id`` — identical to the ``engine_id`` field
-- of the ``[[search_engines]]`` TOML entry (§8.1), so the TOML clock and
-- the score row bind through that single column.
--
-- ``score`` is a bounded integer counter (-50..+50, initial 0): +1 per
-- success, -1 per failure, subject to a 5-minute dedup window and a
-- daily ±3 cap. ``consecutive_fails`` drives the hard-disable rule (20
-- straight failures snap the score to <= -25). ``manual_state`` carries
-- the user override (``auto`` / ``forced_on`` / ``forced_off``).
-- ``total_calls`` / ``total_successes`` are cumulative counters kept only
-- for the settings-panel success-rate readout — never gated by the dedup
-- window or the daily cap.
--
-- Additive only (AGENTS.md §8): a brand-new standalone table, no existing
-- column / index / FK is touched, so old databases upgrade in place.
--
-- Idempotence: ``CREATE TABLE IF NOT EXISTS`` makes a re-run a no-op, and
-- the migration runner records applied ids in ``_qai_schema_migrations``
-- and skips already-applied files (see
-- ``qai.platform.persistence.migrations.MigrationRunner``). The runner
-- manages BEGIN/COMMIT — this file MUST NOT contain transaction
-- statements.
--
-- A ``[[search_engines]]`` entry removed from the TOML does NOT drop its
-- row here (history is preserved); manual cleanup is
-- ``DELETE FROM search_engine_score WHERE engine_id = ?``.
-- =====================================================================

CREATE TABLE IF NOT EXISTS search_engine_score (
    engine_id         TEXT    NOT NULL PRIMARY KEY,
    score             INTEGER NOT NULL DEFAULT 0,
    consecutive_fails INTEGER NOT NULL DEFAULT 0,
    last_recorded_ts  INTEGER NOT NULL DEFAULT 0,
    today_date        TEXT    NOT NULL DEFAULT '',
    today_count       INTEGER NOT NULL DEFAULT 0,
    manual_state      TEXT    NOT NULL DEFAULT 'auto',
    total_calls       INTEGER NOT NULL DEFAULT 0,
    total_successes   INTEGER NOT NULL DEFAULT 0,
    updated_at        INTEGER NOT NULL DEFAULT 0
);
