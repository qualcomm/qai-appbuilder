-- ============================================================================
-- Migration 034: chat_participant per-participant config column
--
-- Multi-agent conversations (docs/70-multi-agent/multi-agent-conversation-design.md §16).
-- Adds one nullable column to chat_participant storing a per-participant JSON
-- config blob:
--
--   config_json  TEXT  -- JSON object, e.g.
--                       --   {"allowed_tools": ["read", "grep"], "color": "..."}
--
-- Why: A ``NAMED_AGENT`` participant in a discussion needs a user-customisable
-- allowed-tools set (which tools that role may invoke) and a presentation
-- ``color`` (a theme-palette index/token, never a hard-coded colour).  These
-- are stored as a free-form JSON object on the participant row so the domain
-- ``Participant.config`` dict round-trips through persistence.
--
-- NULL by default — existing user / main-agent / sub-agent participants read
-- NULL, which the aggregate maps to its ``config=None`` default.  Done as a
-- standalone ALTER (NOT by editing 030) so existing databases upgrade
-- in-place; the runner applies each file once.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


ALTER TABLE chat_participant ADD COLUMN config_json TEXT;
