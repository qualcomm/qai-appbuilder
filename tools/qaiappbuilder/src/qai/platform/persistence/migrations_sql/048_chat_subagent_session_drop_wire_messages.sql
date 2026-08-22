-- ============================================================================
-- Migration 048: drop chat_subagent_session.wire_messages_json
--
-- SUBAGENT-UNIFY-6: the AUTHORITATIVE structured transcript (messages_json,
-- added in migration 047 — a JSON array of serialised Message dicts) is now the
-- SOLE truth source for a sub-agent's transcript. The detail route serialises
-- it directly and the feed-the-model wire is rebuilt from it on demand via
-- rebuild_history_wire_messages (口径 parity with the main agent's cross-turn
-- rebuild). The legacy flat OpenAI wire_messages_json column is no longer read
-- or written by any runtime path (the take-over canvas, the autonomous loop,
-- the resume rebuild and the detail route all consume messages_json), so it is
-- dropped here — completing migration 047's stated plan ("a later migration
-- drops it once every reader consumes the structured transcript").
--
-- SQLite >= 3.35.0 supports ALTER TABLE DROP COLUMN (the dev/CI venv runs
-- 3.50.x). Standalone ALTER (NOT by editing 030/047). Existing databases
-- upgrade in-place; the runner applies each versioned file exactly once and
-- manages BEGIN/COMMIT -- this file MUST NOT contain transaction statements.
-- ============================================================================


ALTER TABLE chat_subagent_session DROP COLUMN wire_messages_json;
