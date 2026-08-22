-- ============================================================================
-- Migration 037: chat_subagent_session.last_prompt_tokens column
--
-- Persists the standalone sub-agent tab's context-occupancy figure (the most
-- recent round's provider-measured prompt_tokens, replace-last semantics) so
-- the GET sub-agent context badge survives a DB reload. The domain field
-- SubAgentSession.last_prompt_tokens was added (refactor A4) WITHOUT a backing
-- column, so every reload via SqliteSubAgentSessionRepository reconstructed it
-- as None -> the badge always showed 0. Mirrors CodingSession's
-- last_input_tokens (migration 024). NULL = never measured.
--
-- Standalone ALTER (NOT by editing 030). Existing databases upgrade in-place;
-- the runner applies each versioned file exactly once. The runner manages
-- BEGIN/COMMIT -- this file MUST NOT contain transaction statements.
-- ============================================================================


ALTER TABLE chat_subagent_session ADD COLUMN last_prompt_tokens INTEGER;
