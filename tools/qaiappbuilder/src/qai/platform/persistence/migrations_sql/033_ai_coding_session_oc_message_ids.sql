-- ============================================================================
-- Migration 033: ai_coding_session OpenCode native message-id column
--
-- 2-H3 / RE-OC-7 (restart-safe OpenCode native revert).  Adds one nullable
-- column to ai_coding_session storing the OpenCode-native message ids
-- learned per turn (a JSON array of strings, in turn order):
--
--   oc_message_ids  TEXT  -- JSON array, e.g. ["msg_aaa","msg_bbb"]
--
-- Why: V1 forwarded the frontend-supplied OpenCode ``messageID`` straight
-- to OpenCode's native ``POST /session/{id}/revert``, so revert
-- worked for ANY historical turn.  V2's OpenCode adapter only cached the
-- learned message ids in-process, so after a daemon restart / for a
-- restored session the native revert silently no-op'd.  Persisting the
-- ordered id list on the session lets the revert / rewind use cases
-- resolve a ``marker_index`` to the right native ``messageID`` even after
-- a restart.
--
-- NULL by default — a fresh / non-OpenCode session reads NULL, which the
-- aggregate maps to its empty-tuple default.  Done as a standalone ALTER
-- (NOT by editing 004) so existing databases upgrade in-place; the runner
-- applies each file once.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


ALTER TABLE ai_coding_session ADD COLUMN oc_message_ids TEXT;
