-- ============================================================================
-- Migration 030: sub-agent session persistence + participant abstraction
--
-- Creates 2 tables (schema doc qai-db-schema.md §2.6 ~ §2.7):
--   chat_subagent_session  — persists a sub-agent's OpenAI wire context so its
--                            collapsed fold blocks / continuation survive a page
--                            reload or conversation switch (tracks parent chat).
--   chat_participant       — a generic "who is speaking" abstraction for a
--                            conversation. Today it carries sub-agents; in the
--                            future it carries the named roles of a multi-agent
--                            conversation. This is ORTHOGONAL to chat_message.role
--                            (system/user/assistant/tool): role = the message's
--                            kind, participant = the speaker identity. The two
--                            dimensions are linked from the message side via
--                            chat_message.sender_id (added in migration 031).
--
-- Done as a standalone CREATE migration (NOT by editing 002) so existing
-- databases that already applied 002 are upgraded in-place; the migration
-- runner applies each versioned file exactly once.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- chat_subagent_session: persisted wire context for one sub-agent.
--
-- parent_conversation_id is a hard FK to chat_conversation(id) ON DELETE CASCADE:
--   when the parent conversation is deleted, all of its sub-agent sessions are
--   removed too (they have no meaning outside their parent conversation).
--
-- parent_message_id is a DELIBERATELY SOFT reference (plain TEXT, NO FK):
--   chat_message rows are rewritten with a DELETE-then-INSERT strategy on each
--   conversation save, so a hard FK to chat_message(id) would either be violated
--   transiently during the rewrite (with ON DELETE CASCADE wiping sub-agent rows)
--   or block the rewrite. Keeping it a soft ref lets the message rewrite proceed
--   without disturbing sub-agent rows; a stale parent_message_id is harmless.
--
-- status mirrors the sub-agent lifecycle; 'user_owned' marks a sub-agent the
-- user has adopted into a normal conversation. owner distinguishes a session
-- still driven by the main agent vs one the user took over.
--
-- wire_messages_json is the core payload: the sub-agent's OpenAI-wire message
-- history (a JSON array), so the fold blocks can be re-rendered after reload.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_subagent_session (
    id                      TEXT    NOT NULL PRIMARY KEY,
    parent_conversation_id  TEXT    NOT NULL,
    parent_message_id       TEXT,
    subagent_type           TEXT    NOT NULL DEFAULT 'agent',
    title                   TEXT,
    prompt_preview          TEXT    NOT NULL DEFAULT '',
    status                  TEXT    NOT NULL DEFAULT 'running'
                                    CHECK (status IN ('running', 'done', 'error',
                                                      'interrupted', 'user_owned')),
    owner                   TEXT    NOT NULL DEFAULT 'main_agent'
                                    CHECK (owner IN ('main_agent', 'user')),
    wire_messages_json      TEXT    NOT NULL DEFAULT '[]',
    rounds                  INTEGER NOT NULL DEFAULT 0 CHECK (rounds >= 0),
    created_at              TEXT    NOT NULL,
    updated_at              TEXT    NOT NULL,
    FOREIGN KEY (parent_conversation_id) REFERENCES chat_conversation(id) ON DELETE CASCADE
);

-- FK index: list a conversation's sub-agent sessions (hot path)
CREATE INDEX IF NOT EXISTS ix_chat_subagent_session_parent
    ON chat_subagent_session(parent_conversation_id);
-- recency sort within a conversation
CREATE INDEX IF NOT EXISTS ix_chat_subagent_session_parent_updated
    ON chat_subagent_session(parent_conversation_id, updated_at DESC);


-- ----------------------------------------------------------------------------
-- chat_participant: generic participant ("who") of a conversation.
--
-- This is the common base layer for sub-agent persistence today and for
-- multi-agent named-role conversations in the future. role (on chat_message)
-- stays untouched; participant is the orthogonal "speaker identity" dimension.
--
-- conversation_id is a hard FK to chat_conversation(id) ON DELETE CASCADE.
--
-- subagent_session_id links a sub_agent participant to its persisted wire
-- session. It is a hard FK to chat_subagent_session(id) ON DELETE SET NULL:
--   if the sub-agent session is purged, the participant row survives (its
--   display_name / kind are still meaningful for already-rendered history) but
--   loses its session link. user / main_agent / named_agent participants leave
--   this column NULL.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_participant (
    id                   TEXT NOT NULL PRIMARY KEY,
    conversation_id      TEXT NOT NULL,
    kind                 TEXT NOT NULL
                              CHECK (kind IN ('user', 'main_agent', 'sub_agent', 'named_agent')),
    display_name         TEXT NOT NULL DEFAULT '',
    model_id             TEXT,
    persona              TEXT,
    subagent_session_id  TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES chat_conversation(id) ON DELETE CASCADE,
    FOREIGN KEY (subagent_session_id) REFERENCES chat_subagent_session(id) ON DELETE SET NULL
);

-- FK index: list a conversation's participants (hot path)
CREATE INDEX IF NOT EXISTS ix_chat_participant_conversation
    ON chat_participant(conversation_id);
-- filter participants by kind within a conversation (e.g. only sub_agents)
CREATE INDEX IF NOT EXISTS ix_chat_participant_conversation_kind
    ON chat_participant(conversation_id, kind);
