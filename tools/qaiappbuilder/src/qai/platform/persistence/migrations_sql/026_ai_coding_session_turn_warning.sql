-- ============================================================================
-- Migration 026: ai_coding_session turn-count + over-turn-warning columns
--
-- 2-H12 (turn_warning — over-turn-count reminder).  Adds two NOT NULL
-- DEFAULT 0 columns to ai_coding_session so the per-session turn counter
-- and the last-emitted warning threshold survive a daemon restart (V1
-- parity) instead of re-warning from scratch:
--
--   turn_count                  INTEGER — completed streaming turns
--   last_turn_warning_threshold INTEGER — highest warning tier already
--                                          surfaced (20 / 25 / 30 / …)
--
-- V1 anchor: the legacy threshold sequence + warning emission gated on a
-- fresh tier.
--
-- Done as a standalone ALTER migration (NOT by editing 004) so existing
-- databases are upgraded in-place; the runner applies each file once.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


ALTER TABLE ai_coding_session ADD COLUMN turn_count                  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ai_coding_session ADD COLUMN last_turn_warning_threshold INTEGER NOT NULL DEFAULT 0;
