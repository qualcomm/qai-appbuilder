-- ============================================================================
-- Migration 043: cloned_from_id — clone/reset provenance for the three
--                discussion-template families (agent / roster / mode).
--
-- Tail-appends ONE nullable column to each of the three template tables:
--   chat_agent_template.cloned_from_id   TEXT NULL
--   chat_roster_template.cloned_from_id  TEXT NULL
--   chat_mode_template.cloned_from_id    TEXT NULL
--
-- Each records the SOURCE template's id when a template was created by
-- *cloning* another (typically a built-in preset). "Editing a built-in preset"
-- is modelled as: clone it into a user copy (is_builtin=0 +
-- cloned_from_id=preset.id), then edit the copy — the preset stays read-only.
-- A copy can later be RESET: its business fields are restored from the source
-- referenced by cloned_from_id, IN PLACE (the copy keeps its own id /
-- created_at / cloned_from_id, only updated_at bumps) so no dangling reference
-- is ever produced.
--
-- Deliberately NO FOREIGN KEY: the source may be a built-in preset re-seeded
-- with a fresh id across installs, or a user template later deleted; a stale
-- cloned_from_id simply means "reset has no source to restore from" (the reset
-- use case validates the source still resolves and otherwise raises), it never
-- corrupts the copy itself.
--
-- ZERO legacy-data migration (v2.7 §2): each column is nullable with no
-- default, so every pre-existing row reads back NULL (an original, not a clone)
-- and all existing list / create / update / delete behaviour is byte-for-byte
-- unchanged.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================

ALTER TABLE chat_agent_template ADD COLUMN cloned_from_id TEXT;
ALTER TABLE chat_roster_template ADD COLUMN cloned_from_id TEXT;
ALTER TABLE chat_mode_template ADD COLUMN cloned_from_id TEXT;
