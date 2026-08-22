-- ============================================================================
-- Migration 074: wall-clock intent for the scheduled-task feature
--
-- Adds two columns to ``scheduling_job`` so a schedule can express what a
-- UTC-only payload cannot:
--
--   * ``start_at``           — ISO-8601 UTC timestamp of a RECURRING task's
--                              FIRST fire. Without it an interval task's first
--                              run was always one full interval after creation,
--                              so "every 2h, starting 09:00" was unexpressible.
--                              ``NULL`` ⇒ pre-migration behaviour (first run one
--                              interval out / next cron slot).
--   * ``tz_offset_minutes``  — east-of-UTC offset (e.g. 480 for +08:00) of the
--                              zone whose WALL CLOCK the user meant. Cron fields
--                              ("0 7 * * *") name a wall clock, not a UTC
--                              instant: anchoring them in UTC makes a "07:00
--                              daily" task fire at 15:00 local in +08:00. The
--                              offset travels as DATA (never read from the
--                              server's local zone) so next-run computation stays
--                              pure and deterministic under test, and the same
--                              row computes identically on any host.
--                              ``NULL`` ⇒ legacy UTC anchoring (unchanged).
--
-- Additive only (AGENTS.md §8): two NULLABLE columns on an existing table. Every
-- pre-migration row reads ``NULL`` for both and keeps its exact previous
-- behaviour (the parser/scheduler treat ``None`` as "no explicit first run" /
-- "anchor in UTC"). No existing column, index, or row is touched — same additive
-- pattern as migrations 069 (``model_id``) and 070 (whitelists).
--
-- Idempotence: the runner records applied ids in ``_qai_schema_migrations`` and
-- skips already-applied files (SQLite ``ADD COLUMN`` has no ``IF NOT EXISTS``;
-- the applied-set is the guard). The runner manages BEGIN/COMMIT — this file
-- MUST NOT contain transaction statements.
-- ============================================================================

ALTER TABLE scheduling_job
    ADD COLUMN start_at TEXT;

ALTER TABLE scheduling_job
    ADD COLUMN tz_offset_minutes INTEGER;
