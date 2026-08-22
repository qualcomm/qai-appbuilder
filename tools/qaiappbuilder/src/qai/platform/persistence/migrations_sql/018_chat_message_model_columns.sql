-- ============================================================================
-- Migration 018: chat_message model columns
--
-- Adds two nullable TEXT columns to chat_message so an assistant turn
-- records the model that produced it (V1 parity: msg.model_id /
-- msg.model_provider). This lets a reloaded history bubble show the real
-- model display name even after the user switches models, instead of
-- tracking the current selection.
--   model_id        canonical model id (e.g. "local::foo" / "claude-...")
--   model_provider  provider slug ("" / NULL for local / unknown)
-- NULL means the row predates this column or is a non-assistant turn.
--
-- Done as a standalone ALTER migration (NOT by editing 002) so existing
-- databases that already applied 002 are upgraded in-place; the schema
-- migration runner applies each versioned file exactly once.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


ALTER TABLE chat_message ADD COLUMN model_id TEXT;
ALTER TABLE chat_message ADD COLUMN model_provider TEXT;
