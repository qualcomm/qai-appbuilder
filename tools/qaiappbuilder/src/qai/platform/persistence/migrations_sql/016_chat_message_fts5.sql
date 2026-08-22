-- ============================================================================
-- Migration 016: chat_message FTS5 full-text search index
--
-- Restores full-text search over chat_message.content_text + snippet
-- highlighting, originally provided by the legacy ``messages_fts`` virtual table + the
-- ``snippet(messages_fts, 0, '<mark>', '</mark>', '...', 32)`` query it
-- exposed.
--
-- The original PR-021 chat schema (002_create_chat_schema.sql §10.5)
-- deliberately deferred this virtual table ("adapters will fall back to
-- LIKE-based search"). That left ``GET /api/conversations/search`` unable
-- to (a) match message *body* text and (b) return highlighted snippets —
-- a user-perceivable regression versus V1, whose sidebar search panel
-- highlights matched terms inside the conversation preview. This migration
-- closes that gap.
--
-- This migration is purely additive — it creates one virtual table and
-- three triggers; no DROP / ALTER on chat_message or any other PR-021
-- artefact. 002_create_chat_schema.sql remains the source of truth for the
-- chat_message base table.
--
-- External-content pattern
-- ------------------------
-- ``content='chat_message'`` + ``content_rowid='rowid'`` makes the FTS
-- table an *external-content* index: the tokenized terms live in the FTS
-- shadow tables but the column values are read back from chat_message at
-- query time (no duplicate storage of content_text). This mirrors V1's
-- ``content='messages', content_rowid='rowid'`` choice.
--
-- Tokenizer
-- ---------
-- ``unicode61`` matches V1. It tokenizes on
-- Unicode code-point class boundaries; CJK content is searched via the
-- bigram pre-processing the adapter applies to the *query* (mirroring
-- V1's query-side tokenization), so a CJK substring still matches.
--
-- Triggers
-- --------
-- Standard external-content sync triggers (FTS5 docs §4.4.3):
--   * AFTER INSERT  → INSERT the new row into the FTS index;
--   * AFTER DELETE  → 'delete' op with the OLD payload;
--   * AFTER UPDATE  → 'delete' OLD then INSERT NEW.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- chat_message_fts: full-text search index over chat_message.content_text.
-- conversation_id is carried UNINDEXED so the search query can JOIN back to
-- chat_conversation without a second lookup.
-- ----------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS chat_message_fts USING fts5(
    content_text,
    conversation_id UNINDEXED,
    role UNINDEXED,
    content='chat_message',
    content_rowid='rowid',
    tokenize='unicode61'
);


-- ----------------------------------------------------------------------------
-- Triggers — keep chat_message_fts in lock-step with chat_message.
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ----------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS chat_message_ai
AFTER INSERT ON chat_message
BEGIN
    INSERT INTO chat_message_fts(rowid, content_text, conversation_id, role)
    VALUES (NEW.rowid, NEW.content_text, NEW.conversation_id, NEW.role);
END;


CREATE TRIGGER IF NOT EXISTS chat_message_ad
AFTER DELETE ON chat_message
BEGIN
    INSERT INTO chat_message_fts(chat_message_fts, rowid, content_text, conversation_id, role)
    VALUES ('delete', OLD.rowid, OLD.content_text, OLD.conversation_id, OLD.role);
END;


CREATE TRIGGER IF NOT EXISTS chat_message_au
AFTER UPDATE ON chat_message
BEGIN
    INSERT INTO chat_message_fts(chat_message_fts, rowid, content_text, conversation_id, role)
    VALUES ('delete', OLD.rowid, OLD.content_text, OLD.conversation_id, OLD.role);
    INSERT INTO chat_message_fts(rowid, content_text, conversation_id, role)
    VALUES (NEW.rowid, NEW.content_text, NEW.conversation_id, NEW.role);
END;
