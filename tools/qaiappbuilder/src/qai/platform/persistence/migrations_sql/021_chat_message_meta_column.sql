-- ============================================================================
-- Migration 021: chat_message meta_json column (V1-parity reload extras)
--
-- Adds one nullable JSON column to chat_message so the client-renderable
-- extras of an assistant turn survive a page reload / conversation switch,
-- matching V1 (the legacy _row_to_message promotes these from
-- the messages.meta JSON blob):
--   request_id        prompt-snapshot id -> re-shows the "Prompt Snapshot" button
--   image_url         persisted image URL -> re-renders the image preview
--   perf              {ttft_ms, total_ms, ...} -> re-renders the perf line
--   subAgentBlocks    sub-agent fold blocks -> re-renders the collapsed blocks
--   tool_full_output  complete tool output -> re-renders the full-output tab
--   tool_truncated    truncation flag -> re-renders the truncation badge
--   tool_output_size  byte count -> re-renders the size badge
-- Stored as a free-form JSON object; NULL for turns that carried no extras
-- (older rows / plain text turns).  The column suffix _json follows schema
-- doc A0.4 convention.
--
-- Done as a standalone ALTER migration (NOT by editing 002) so existing
-- databases that already applied 002 are upgraded in-place; the schema
-- migration runner applies each versioned file exactly once.
--
-- runner manages BEGIN/COMMIT -- file MUST NOT contain them.
-- ============================================================================


ALTER TABLE chat_message ADD COLUMN meta_json TEXT;
