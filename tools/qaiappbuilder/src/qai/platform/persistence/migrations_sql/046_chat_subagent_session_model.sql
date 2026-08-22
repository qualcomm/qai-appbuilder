-- ============================================================================
-- Migration 046: chat_subagent_session.model_id / model_provider columns
--
-- Persists the model a sub-agent runs with as the SINGLE source of truth for
-- the context-budget denominator (State-Truth-First 铁律 1 / 铁律 4). A
-- sub-agent defaults to its PARENT's model at spawn, but the user may switch
-- THIS sub-agent's model independently in its standalone tab. Without a backing
-- column the budget denominator could only come from the front-end passing the
-- parent/active tab's model id — the misalignment this column fixes. The
-- SubAgentSession.model_id / model_provider domain fields carry it; every budget
-- read (cold-open GET sub-agent detail, the live LIVE frame, take-over) now
-- resolves the window from this one truthful place.
--
-- model_id stores the RAW selected id (any ``local::`` prefix preserved — the
-- window resolvers strip it, 口径 parity with model_hint). model_provider
-- disambiguates an identical model_id exposed by different providers (e.g. the
-- same claude-* id under provider_a 128K vs cloud LLM service 200K). Both nullable; an
-- existing row (no model recorded) reads back NULL → None, and the budget
-- readers fall back to their prior id/family default (no regression).
--
-- Standalone ALTER (NOT by editing 030). Existing databases upgrade in-place;
-- the runner applies each versioned file exactly once. The runner manages
-- BEGIN/COMMIT -- this file MUST NOT contain transaction statements.
-- ============================================================================


ALTER TABLE chat_subagent_session ADD COLUMN model_id TEXT;
ALTER TABLE chat_subagent_session ADD COLUMN model_provider TEXT;
