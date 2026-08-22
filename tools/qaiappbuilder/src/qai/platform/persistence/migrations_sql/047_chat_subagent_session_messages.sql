-- ============================================================================
-- Migration 047: chat_subagent_session.messages_json column
--
-- Full-unification rewrite: the sub-agent now persists a STRUCTURED transcript
-- (a list of first-class Message objects — the SAME shape the main agent stores
-- into Conversation.messages) as the AUTHORITATIVE display + replay source,
-- instead of relying on the flat OpenAI wire_messages_json being reverse-folded
-- by the route layer (_wire_to_messages). The detail route serialises this
-- column directly; the feed-the-model wire is rebuilt from it via
-- rebuild_history_wire_messages (口径 parity with the main agent's cross-turn
-- rebuild). The SubAgentSession.messages domain field carries it.
--
-- Stored as a JSON array of serialised Message dicts (id / role / content /
-- created_at / parent_id / tool_calls / tool_results / usage / model_id /
-- model_provider / meta / sender_id) — the SAME serialisation the
-- conversation_repository uses for Conversation.messages, so one mapper covers
-- both. DEFAULT '[]' so an existing row (no structured transcript yet) reads
-- back an empty list → the GET falls back to the wire-derived path until a
-- structured run repopulates it (no regression). The flat wire_messages_json
-- column is RETAINED for now (the feed-the-model wire is still recorded there
-- during the double-write phase); a later migration drops it once every reader
-- consumes the structured transcript.
--
-- Standalone ALTER (NOT by editing 030). Existing databases upgrade in-place;
-- the runner applies each versioned file exactly once. The runner manages
-- BEGIN/COMMIT -- this file MUST NOT contain transaction statements.
-- ============================================================================


ALTER TABLE chat_subagent_session ADD COLUMN messages_json TEXT NOT NULL DEFAULT '[]';
