-- ============================================================================
-- Migration 038: chat_roster_template — reusable multi-agent discussion rosters
--
-- Creates 1 table (schema doc qai-db-schema.md §2.8):
--   chat_roster_template  — a named, reusable bundle of discussion role
--                           definitions (a "team": e.g. Architect / Developer /
--                           Tester) that a user can preview + import into any
--                           conversation, so a roster need not be rebuilt from
--                           scratch every time. PURE V2 enhancement (V1 has no
--                           multi-agent discussion at all).
--
-- A roster template is a SINGLE-ROW aggregate: the whole team (its member role
-- definitions) lives in one row's ``members_json`` JSON array, mirroring how
-- ``chat_participant`` keeps its per-row config in ``config_json`` (no child
-- table, no DELETE-then-INSERT rewrite). Each member entry is a plain object:
--   {"display_name": str, "model_id": str|null, "persona": str|null,
--    "config": {"allowed_tools": [str], "color": int|str}|null}
-- — i.e. exactly the fields needed to instantiate a ``kind=named_agent``
-- ``chat_participant`` row when the template is applied to a conversation.
--
-- is_builtin (0/1) marks factory-seeded preset templates (seeded by the install
-- pipeline from factory/db_staging/chat_roster_template.jsonl) so the UI can
-- distinguish "preset" from "my saved" templates; built-ins are NOT bound to any
-- conversation (no FK) — they are a global, conversation-independent library,
-- unlike chat_participant which is strictly conversation-scoped.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================

CREATE TABLE IF NOT EXISTS chat_roster_template (
    id            TEXT    NOT NULL PRIMARY KEY,
    name          TEXT    NOT NULL DEFAULT '',
    description   TEXT    NOT NULL DEFAULT '',
    members_json  TEXT    NOT NULL DEFAULT '[]',
    is_builtin    INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0, 1)),
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_chat_roster_template_builtin
    ON chat_roster_template(is_builtin);
