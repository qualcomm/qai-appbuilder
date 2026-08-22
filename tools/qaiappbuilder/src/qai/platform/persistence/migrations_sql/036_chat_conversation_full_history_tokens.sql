-- ============================================================================
-- Migration 036: chat_conversation.full_history_tokens column
--
-- Running counter of the provider-measured full (uncompressed) history prompt
-- token size, used by GET /context as the "before" figure of the compaction
-- badge ("~205K → ~45K · saved N%"). Cloud usage only measures the actually-
-- sent (post-compaction) wire, so we maintain this counter from provider
-- measurements per turn. NULL = legacy / never measured (derived on read).
--
-- Standalone ALTER (NOT by editing 002). The runner manages BEGIN/COMMIT —
-- this file MUST NOT contain transaction statements.
-- ============================================================================


ALTER TABLE chat_conversation ADD COLUMN full_history_tokens INTEGER;
