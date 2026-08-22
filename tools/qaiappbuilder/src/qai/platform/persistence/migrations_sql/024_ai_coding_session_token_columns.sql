-- ============================================================================
-- Migration 024: ai_coding_session cumulative token / context columns
--
-- U-010 / 2-H2 (token + context full chain).  Adds six nullable-default
-- columns to ai_coding_session so the aggregate's per-session usage
-- counters survive a process restart (V1 parity):
--
--   total_input_tokens   INTEGER  — cumulative input tokens (all turns)
--   total_output_tokens  INTEGER  — cumulative output tokens (all turns)
--   total_tool_calls     INTEGER  — cumulative tool-use blocks (all turns)
--   last_input_tokens    INTEGER  — most-recent turn input tokens (current
--                                   context-occupancy approximation)
--   context_window       INTEGER  — model context-window size (e.g. 200000)
--   total_cost           REAL     — cumulative cost (OC; CC leaves 0.0)
--
-- Done as a standalone ALTER migration (NOT by editing 004) so existing
-- databases that already applied 004 are upgraded in-place; the schema
-- migration runner applies each versioned file exactly once.
--
-- NOT NULL DEFAULT 0 — a fresh / never-streamed session reads concrete
-- zeros, mirroring the aggregate's non-None defaults.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


ALTER TABLE ai_coding_session ADD COLUMN total_input_tokens  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ai_coding_session ADD COLUMN total_output_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ai_coding_session ADD COLUMN total_tool_calls    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ai_coding_session ADD COLUMN last_input_tokens   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ai_coding_session ADD COLUMN context_window      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ai_coding_session ADD COLUMN total_cost          REAL    NOT NULL DEFAULT 0.0;
