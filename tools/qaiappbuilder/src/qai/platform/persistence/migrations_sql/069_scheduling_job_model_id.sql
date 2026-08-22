-- ============================================================================
-- Migration 069: Per-task model id for the scheduled-task feature
--
-- Adds ``model_id`` to ``scheduling_job``. A scheduled task captures the model
-- id of the conversation it was created in, so its later unattended run uses
-- the SAME model the user picked — rather than falling back to a default that
-- may have no configured endpoint (the "[no LLM endpoint configured]" empty
-- run). ``NULL`` means "no model captured" — the runner then falls back to the
-- system default model resolver.
--
-- Additive only (AGENTS.md §8): one nullable column on an existing table; all
-- pre-migration rows read ``NULL`` and the runner's default-model fallback
-- keeps them working. No existing column / index is touched.
--
-- Standalone migration (NOT by editing 068): 068 already shipped/applied on
-- existing test databases, so the column is added in-place here. Idempotence:
-- the runner records applied ids in ``_qai_schema_migrations`` and skips
-- already-applied files (SQLite ``ADD COLUMN`` has no ``IF NOT EXISTS``; the
-- applied-set is the guard). The runner manages BEGIN/COMMIT — this file MUST
-- NOT contain transaction statements.
-- ============================================================================

ALTER TABLE scheduling_job
    ADD COLUMN model_id TEXT;
