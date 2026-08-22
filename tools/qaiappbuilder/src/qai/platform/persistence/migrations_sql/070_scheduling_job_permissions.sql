-- ============================================================================
-- Migration 070: Per-task tool / skill whitelist for the scheduled-task feature
--
-- Adds ``enabled_tools`` and ``enabled_skills`` to ``scheduling_job``. Each is
-- a JSON array string (e.g. ``["read","grep"]``) naming the tools / skills the
-- unattended run is restricted to. ``NULL`` (or an empty array) means "no
-- restriction" — the runner uses the full default tool set and skill catalog,
-- exactly the pre-migration behaviour. When non-empty, the session-level runner
-- derives the complement (full set minus whitelist) as the turn's disabled set.
--
-- Additive only (AGENTS.md §8): two nullable columns on an existing table; all
-- pre-migration rows read ``NULL`` and the runner's "empty = no restriction"
-- rule keeps them working. No existing column / index is touched — same
-- additive pattern as migration 069 (``model_id``).
--
-- Standalone migration (NOT by editing 068/069): those already shipped/applied
-- on existing test databases, so the columns are added in-place here.
-- Idempotence: the runner records applied ids in ``_qai_schema_migrations`` and
-- skips already-applied files (SQLite ``ADD COLUMN`` has no ``IF NOT EXISTS``;
-- the applied-set is the guard). The runner manages BEGIN/COMMIT — this file
-- MUST NOT contain transaction statements.
-- ============================================================================

ALTER TABLE scheduling_job
    ADD COLUMN enabled_tools TEXT;

ALTER TABLE scheduling_job
    ADD COLUMN enabled_skills TEXT;
