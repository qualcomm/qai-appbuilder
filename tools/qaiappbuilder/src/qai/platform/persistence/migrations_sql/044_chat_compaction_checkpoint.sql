-- ============================================================================
-- Migration 044: chat_compaction_checkpoint table (CCD-5, PENDING-WORK.md §1)
--
-- Persists the session-level compaction checkpoint that previously lived ONLY
-- in StreamChatUseCase._compaction_checkpoints (an in-process dict, lost on
-- restart). With this table the checkpoint survives a process restart: after a
-- restart a long conversation's first message no longer falls back to the full
-- verbatim history (which could trip PROMPT_TOO_LONG → force recompaction,
-- several seconds of extra latency) and the differential token baseline is
-- preserved instead of being re-bootstrapped from char estimates.
--
-- One row per conversation (single-row aggregate keyed by conversation_id):
--   anchor_index        — the number of conversation messages already folded
--                         into ``compacted_wire`` (the cross-turn increment is
--                         ``history[anchor_index:]``).
--   compacted_wire_json — json.dumps(checkpoint.compacted_wire); the
--                         already-summarised OpenAI wire head (含 role:tool).
--   estimated_tokens    — bytes-based bootstrap estimate of the compacted wire's
--                         prompt-token size (NULL until first stashed).
--   last_eff_prompt     — TPP-1 post-compaction delta baseline: the last cloud-
--                         measured effective prompt size on a post-compaction
--                         assistant turn (NULL until the first such turn lands).
--   created_at          — wall-clock seconds at checkpoint creation (diagnostic;
--                         CCD-1 tail-append field).
--   anchor_message_id   — id of ``conv.messages[anchor_index-1]`` so the badge's
--                         post-compaction figure ignores pre-compaction usage
--                         (CCD-1 tail-append field; NULL when anchor_index == 0).
--
-- parent FK to chat_conversation(id) ON DELETE CASCADE: when the conversation
-- is deleted, its checkpoint is removed too (it has no meaning outside its
-- conversation). foreign_keys=ON is set by Database._INIT_PRAGMAS, so the
-- conversation-delete path's bare ``DELETE FROM chat_conversation`` cascades
-- here automatically (mirrors chat_subagent_session, migration 030).
--
-- Standalone CREATE migration (NOT by editing 002): existing databases upgrade
-- in-place; the runner applies each versioned file exactly once. The runner
-- manages BEGIN/COMMIT — this file MUST NOT contain transaction statements.
-- ============================================================================


CREATE TABLE IF NOT EXISTS chat_compaction_checkpoint (
    conversation_id     TEXT    NOT NULL PRIMARY KEY,
    anchor_index        INTEGER NOT NULL,
    compacted_wire_json TEXT    NOT NULL DEFAULT '[]',
    estimated_tokens    INTEGER,
    last_eff_prompt     INTEGER,
    created_at          REAL    NOT NULL,
    anchor_message_id   TEXT,
    updated_at          TEXT    NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES chat_conversation(id) ON DELETE CASCADE
);
