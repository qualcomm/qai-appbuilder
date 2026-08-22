-- ============================================================================
-- Migration 002: chat context schema (PR-021)
--
-- Creates 4 tables (schema doc qai-db-schema.md §2.1 ~ §2.4):
--   chat_conversation, chat_message, chat_conversation_tab, chat_experience
--
-- §2.5 chat_context_snapshot is NOT created here (schema doc §2.5 marks
-- it optional; the chat context's GetContextDecisionUseCase is implemented
-- without a snapshot row, so this migration intentionally omits the
-- table to keep the schema minimal).
--
-- §10.5 FTS5 chat_message_fts virtual table: NOT enabled (B2 task spec
-- explicit decision; adapters will fall back to LIKE-based search).
--
-- Cross-context: NO foreign keys (only TEXT soft refs).
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- chat_conversation: aggregate root (one row per conversation history).
-- status literals match domain ConversationStatus (active / archived).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_conversation (
    id          TEXT NOT NULL PRIMARY KEY,
    title       TEXT NOT NULL CHECK (length(title) >= 1 AND length(title) <= 256),
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- list / search hot path (status filter + recency sort)
CREATE INDEX IF NOT EXISTS ix_chat_conversation_status_updated
    ON chat_conversation(status, updated_at DESC);


-- ----------------------------------------------------------------------------
-- chat_message: ordered messages belonging to a conversation.
-- role literals match domain MessageRole.
-- parent_id is a self-referential soft branch link; SET NULL on delete so
-- pruning a parent message does not destroy descendants.
-- media_refs / tool_calls / tool_results stored as JSON arrays (column suffix
-- _json per schema doc §0.4 type convention).
-- position is 0-based within the conversation (UNIQUE with conversation_id).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_message (
    id                 TEXT    NOT NULL PRIMARY KEY,
    conversation_id    TEXT    NOT NULL,
    parent_id          TEXT,
    role               TEXT    NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content_text       TEXT    NOT NULL CHECK (length(content_text) <= 1000000),
    media_refs_json    TEXT    NOT NULL DEFAULT '[]',
    tool_calls_json    TEXT    NOT NULL DEFAULT '[]',
    tool_results_json  TEXT    NOT NULL DEFAULT '[]',
    created_at         TEXT    NOT NULL,
    position           INTEGER NOT NULL CHECK (position >= 0),
    UNIQUE (conversation_id, position),
    FOREIGN KEY (conversation_id) REFERENCES chat_conversation(id) ON DELETE CASCADE,
    -- self-FK for branching; SET NULL preserves descendant when a parent is removed
    FOREIGN KEY (parent_id) REFERENCES chat_message(id) ON DELETE SET NULL
);

-- FK indexes (schema doc §9.6) + branch traversal
CREATE INDEX IF NOT EXISTS ix_chat_message_conversation_id
    ON chat_message(conversation_id);
CREATE INDEX IF NOT EXISTS ix_chat_message_parent
    ON chat_message(parent_id);


-- ----------------------------------------------------------------------------
-- chat_conversation_tab: transient front-end tab state pinned to a conversation.
-- status literals match domain TabStatus (idle / streaming / aborted / closed).
-- "at most one streaming tab per conversation" is enforced by application
-- layer (see Conversation.append_message + StreamAbortRegistryPort);
-- the partial index below documents the hot path used by that lookup.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_conversation_tab (
    id               TEXT NOT NULL PRIMARY KEY,
    conversation_id  TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'idle'
                          CHECK (status IN ('idle', 'streaming', 'aborted', 'closed')),
    created_at       TEXT NOT NULL,
    last_active_at   TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES chat_conversation(id) ON DELETE CASCADE
);

-- FK index + status lookup; partial for the "currently-streaming tab" query
CREATE INDEX IF NOT EXISTS ix_chat_conversation_tab_conversation_status
    ON chat_conversation_tab(conversation_id, status);
CREATE INDEX IF NOT EXISTS ix_chat_conversation_tab_streaming
    ON chat_conversation_tab(conversation_id)
    WHERE status = 'streaming';


-- ----------------------------------------------------------------------------
-- chat_experience: reusable knowledge snippets (replaces data/experiences.db).
-- metadata_json is a free-form JSON object (default '{}').
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_experience (
    id             TEXT NOT NULL PRIMARY KEY,
    category       TEXT NOT NULL CHECK (length(category) >= 1 AND length(category) <= 64),
    content        TEXT NOT NULL CHECK (length(content) >= 1 AND length(content) <= 100000),
    metadata_json  TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_chat_experience_category
    ON chat_experience(category, created_at DESC);
