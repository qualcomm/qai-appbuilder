-- ============================================================================
-- Migration 025: ai_coding_session OpenCode provider / model columns
--
-- 2-H10 (OC current_provider / current_model persistence).  Adds two
-- nullable columns to ai_coding_session so an OpenCode session's runtime
-- provider/model selection survives a daemon restart (V1 parity) instead
-- of resetting to OpenCode's server default:
--
--   oc_current_provider  TEXT  — the OpenCode providerID chosen for this
--                                 session (e.g. "anthropic" / "opencode")
--   oc_current_model     TEXT  — the OpenCode modelID chosen for this
--                                 session (empty / NULL = server default)
--
-- V1 anchor: ``current_provider`` / ``current_model`` on the OC session record.
--
-- Both NULL by default — a fresh / non-OpenCode session reads NULL, which
-- the aggregate maps to its ``None`` defaults (use the server default).
-- Done as a standalone ALTER migration (NOT by editing 004) so existing
-- databases are upgraded in-place; the runner applies each file once.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


ALTER TABLE ai_coding_session ADD COLUMN oc_current_provider TEXT;
ALTER TABLE ai_coding_session ADD COLUMN oc_current_model    TEXT;
