-- ============================================================================
-- Migration 045: chat_subagent_session.allow_spawn column
--
-- Persists whether a sub-agent was GRANTED the ability to spawn its own
-- sub-agents at spawn time (i.e. the spawning main agent's per-tab
-- "allow first-level sub-agents to spawn their own sub-agents" switch —
-- allow_child_spawn — was ON). The domain field
-- SubAgentSession.allow_spawn carries it; without a backing column every
-- reload via SqliteSubAgentSessionRepository reconstructed it as False, so a
-- USER taking the sub-agent over in a standalone tab could not see that the
-- main agent had authorised it to spawn (the "allow this sub-agent to create
-- sub-agents" toggle stayed dark). With this column the GET sub-agent detail
-- endpoint surfaces allow_spawn and the front-end DEFAULTS the toggle ON for
-- an authorised sub-agent (the user may still turn it off — it is a default,
-- not a lock). Stored as INTEGER 0/1; NULL / 0 = not granted (the historical
-- hard recursion guard) so existing rows upgrade cleanly.
--
-- Standalone ALTER (NOT by editing 030). Existing databases upgrade in-place;
-- the runner applies each versioned file exactly once. The runner manages
-- BEGIN/COMMIT -- this file MUST NOT contain transaction statements.
-- ============================================================================


ALTER TABLE chat_subagent_session ADD COLUMN allow_spawn INTEGER NOT NULL DEFAULT 0;
