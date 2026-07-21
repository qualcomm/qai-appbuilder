-- ============================================================================
-- Migration 057: chat_conversation.detected_model_json column
--
-- Persisted promote-ready detection result for a conversation, so the
-- "Promote to App Builder" CTA can be surfaced with ZERO on-open disk scans
-- and refreshed once per turn (instead of the old high-frequency, every-message
-- global scan). The backend detects at turn end (``_finalize_assistant_message``):
-- it extracts the model workspace path (``C:\WoS_AI\<model>``) from the turn's
-- final summary text, scans it for precision variants, and stores the result
-- here as a JSON object:
--     {"workdir": "C:\\WoS_AI\\<model>",
--      "variants": [{"precision": "...", "label": "..."}],
--      "checked_at": "<iso8601>"}
-- An empty ``workdir`` / empty ``variants`` records "checked, nothing to
-- promote"; NULL = never detected (legacy / forward-compatible default).
--
-- Standalone ALTER (NOT by editing 002). The runner manages BEGIN/COMMIT —
-- this file MUST NOT contain transaction statements.
-- ============================================================================


ALTER TABLE chat_conversation ADD COLUMN detected_model_json TEXT;
