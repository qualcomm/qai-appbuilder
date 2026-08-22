-- ============================================================================
-- Migration 013: chat_message token-usage column (P1-4)
--
-- Adds one nullable JSON column to chat_message so an assistant turn's
-- token usage (from the terminal stream END frame) survives page reload.
-- Stored as the normalized OpenAI usage dict
--   {prompt_tokens, completion_tokens, total_tokens,
--    cache_read_tokens?, cache_write_tokens?}
-- (see qai.chat.infrastructure.llm_stream._extract_usage).
-- NULL means the message carried no usage block (older rows / non-assistant
-- roles); the column suffix _json follows schema doc §0.4 convention.
--
-- Done as a standalone ALTER migration (NOT by editing 002) so existing
-- databases that already applied 002 are upgraded in-place; the schema
-- migration runner applies each versioned file exactly once.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


ALTER TABLE chat_message ADD COLUMN usage_json TEXT;
