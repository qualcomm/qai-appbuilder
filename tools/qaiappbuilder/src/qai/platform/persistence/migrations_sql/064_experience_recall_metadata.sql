-- ============================================================================
-- Migration 064: Experience recall metadata for chat_experience
--
-- Adds ``updated_at``, ``use_count`` and ``importance`` to ``chat_experience``
-- so the agent-editable experience store (``remember_experience`` /
-- ``recall_experience`` tools) can:
--   * track when an experience was last edited (``updated_at``), distinct
--     from the immutable ``created_at``;
--   * reserve a recall-frequency counter (``use_count``) for future ranking /
--     pruning — RESERVED: no code increments it yet (recall is a read-only hot
--     path; adding a write there would risk the recall-latency SLO), so it
--     stays 0 until a deliberate write path is added;
--   * bias recall ranking by a caller-supplied salience (``importance``,
--     0.0..1.0, default 0.5).
--
-- These close the V1 knowledge-recall parity gap flagged in the S9 audit
-- (docs .../gaps: chat_experience had no use_count / updated_at columns).
--
-- Additive only (AGENTS.md §8): three new columns on an existing table, all
-- with defaults so pre-migration rows remain valid. ``updated_at`` is
-- nullable (a row never edited since creation reads NULL and the repository
-- falls back to ``created_at``); ``use_count`` / ``importance`` carry NOT
-- NULL defaults so existing rows read as unused, medium-salience. No existing
-- column, index, FK, or the experience_fts virtual table / triggers
-- (migration 009) is touched — the FTS index only mirrors ``content``, which
-- this migration does not alter.
--
-- Idempotence: the migration runner records applied ids in
-- ``_qai_schema_migrations`` and skips already-applied files, so a repeated
-- process start never replays this file. SQLite's ``ADD COLUMN`` has no
-- ``IF NOT EXISTS`` clause; the runner's applied-set is the only guard we
-- need.
--
-- Standalone migration (NOT by editing 002 or 009): existing databases
-- upgrade in-place. The runner manages BEGIN/COMMIT — this file MUST NOT
-- contain transaction statements.
-- ============================================================================

ALTER TABLE chat_experience
    ADD COLUMN updated_at TEXT;

ALTER TABLE chat_experience
    ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE chat_experience
    ADD COLUMN importance REAL NOT NULL DEFAULT 0.5;
