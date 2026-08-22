-- ============================================================================
-- Migration 020: chat_conversation meta_json column (V1-parity channel source)
--
-- Adds one nullable JSON column to chat_conversation so the channel source
-- (wechat / feishu) is persisted and surfaced in the sidebar icon.
-- V1 parity: the legacy history store's upsert_conversation stored
--   meta={"source":"wechat","wechat_user_id":...} in the conversation row.
-- Stored as a free-form JSON object; NULL for conversations created via the
-- web UI (no channel source).  The column suffix _json follows schema doc
-- §0.4 convention.
--
-- Done as a standalone ALTER migration (NOT by editing 002) so existing
-- databases that already applied 002 are upgraded in-place; the schema
-- migration runner applies each versioned file exactly once.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


ALTER TABLE chat_conversation ADD COLUMN meta_json TEXT;
