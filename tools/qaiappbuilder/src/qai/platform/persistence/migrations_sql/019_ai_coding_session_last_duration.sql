-- ============================================================================
-- Migration 019: ai_coding_session.last_duration_s column
--
-- Adds a nullable REAL column to ai_coding_session for V1 parity:
-- the legacy CC/OC streaming pipeline emitted a per-turn ``duration_s``
-- on every ``done`` SSE frame (rounded to 1 decimal place).
-- The user-facing surface in V1 displayed "本次会话耗时 X.X s" on the
-- session badge / header.
--
-- Persisting the most-recent turn's duration on the aggregate lets the
-- REST ``CodingSessionResponse`` expose ``last_duration_s`` for
-- after-the-fact display (e.g. on session list reload or panel reopen)
-- and lets the SSE ``done`` frame carry the value for live UI.
--
-- NULL = "no completed streaming turn yet" (fresh / never-streamed
-- session) — matches the V1 absence semantics on the very first frame
-- before a turn completes.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


ALTER TABLE ai_coding_session ADD COLUMN last_duration_s REAL;
