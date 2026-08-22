-- ============================================================================
-- Migration 009: chat_experience FTS5 search index (PR-094 §17.5 #13 / §3.3 A-14)
--
-- Restores full-text search over chat_experience, originally provided by
-- the legacy ``experiences_fts`` virtual table. The
-- S9 audit (§3.3 A-14) flagged the loss of FTS5 search as a measurable
-- knowledge-recall regression: without an FTS index the
-- SqliteExperienceRepository.search_fulltext call must fall back to LIKE
-- scans, which on a multi-thousand-row corpus blow past the 200ms p95 SLO
-- the legacy front-end depended on.
--
-- This migration is purely additive — it creates one virtual table and
-- three triggers; no DROP / ALTER on chat_experience or any other PR-021
-- artefact. Migration 002_create_chat_schema.sql remains the source of
-- truth for the chat_experience base table.
--
-- Tokenizer choice
-- ----------------
-- ``porter unicode61`` (porter stemming over the unicode61 tokenizer)
-- mirrors the chat-history FTS5 semantics documented in the legacy
-- harness (docs/60-agent-harness/harness-engineering-integration-steps.md
-- :1130). Porter stemming gives English plural / tense folding; unicode61
-- tokenizes Unicode code points so CJK content remains searchable on word
-- boundaries.
--
-- Triggers
-- --------
-- Three triggers (insert/update/delete on chat_experience) keep the FTS
-- table in sync via the standard ``content`` rowid pattern:
--
--   * AFTER INSERT  → INSERT into the FTS table mirroring the new row;
--   * AFTER UPDATE  → DELETE then INSERT the FTS row (FTS5 'delete' op
--                      requires the OLD payload so we pass it explicitly);
--   * AFTER DELETE  → DELETE the FTS row.
--
-- The FTS table is content-less (no ``content='chat_experience'``) so we
-- pay the storage cost of a duplicate column but get O(1) row rebuilds
-- on UPDATE and never have to rebuild the FTS index from scratch.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- experience_fts: full-text search index over chat_experience.content
-- ----------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS experience_fts USING fts5(
    experience_id UNINDEXED,
    content,
    tokenize='porter unicode61'
);


-- ----------------------------------------------------------------------------
-- Triggers — keep experience_fts in lock-step with chat_experience.
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ----------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS chat_experience_ai
AFTER INSERT ON chat_experience
BEGIN
    INSERT INTO experience_fts(experience_id, content)
    VALUES (NEW.id, NEW.content);
END;


CREATE TRIGGER IF NOT EXISTS chat_experience_ad
AFTER DELETE ON chat_experience
BEGIN
    DELETE FROM experience_fts WHERE experience_id = OLD.id;
END;


CREATE TRIGGER IF NOT EXISTS chat_experience_au
AFTER UPDATE ON chat_experience
BEGIN
    DELETE FROM experience_fts WHERE experience_id = OLD.id;
    INSERT INTO experience_fts(experience_id, content)
    VALUES (NEW.id, NEW.content);
END;
