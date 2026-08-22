-- ============================================================================
-- Migration 012: ai_coding_session dual-channel notify binding columns
--
-- Adds two nullable columns to ai_coding_session (legacy parity): when set,
-- WebUI turns are mirror-pushed to the bound WeChat user / Feishu open-id.
-- Both NULL means no binding.  Backs the
-- POST /sessions/{id}/wechat_notify + .../feishu_notify routes
-- (V1 parity).
--
-- Done as a standalone ALTER migration (NOT by editing 004) so existing
-- databases that already applied 004 are upgraded in-place; the schema
-- migration runner applies each versioned file exactly once.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


ALTER TABLE ai_coding_session ADD COLUMN wechat_notify_user_id TEXT;
ALTER TABLE ai_coding_session ADD COLUMN feishu_notify_user_id TEXT;
