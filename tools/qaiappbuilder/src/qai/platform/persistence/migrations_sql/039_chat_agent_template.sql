-- ============================================================================
-- Migration 039: chat_agent_template — reusable single-role (agent) templates
--
-- Creates 1 table (schema doc qai-db-schema.md §2.9):
--   chat_agent_template  — a named, reusable definition of a SINGLE discussion
--                          role (an "agent": e.g. 资深架构师 / 全栈开发 / 严谨测试)
--                          that a user can preview + import into any conversation
--                          (or pull into a team), so a frequently-used role need
--                          not be re-typed every time. PURE V2 enhancement (V1
--                          has no multi-agent discussion at all).
--
-- The three-tier template system (design §27):
--   single role (chat_agent_template, this)  →  team (chat_roster_template,
--   migration 038)  →  mode (chat_mode_template). They are orthogonal: a
--   single-role template is the *smallest reusable unit* — "what one role looks
--   like". A team embeds copies of role definitions (decision 1, §27.2); the
--   single-role library is a convenient SOURCE to copy from, not a runtime
--   reference. "Applying" a single-role template to a conversation instantiates
--   exactly one ``kind=named_agent`` chat_participant row.
--
-- Each row carries exactly the fields needed to instantiate a participant:
--   id / name / description (library-level metadata) +
--   display_name / model_id / persona / config_json
--     (config_json: {"allowed_tools": [str], "color": int|str}|null)
--
-- is_builtin (0/1) marks factory-seeded preset agents (seeded by the install
-- pipeline from factory/db_staging/chat_agent_template.jsonl) so the UI can
-- distinguish "preset" from "my saved" agents; built-ins are NOT bound to any
-- conversation (no FK) — they are a global, conversation-independent library,
-- unlike chat_participant which is strictly conversation-scoped.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================

CREATE TABLE IF NOT EXISTS chat_agent_template (
    id            TEXT    NOT NULL PRIMARY KEY,
    name          TEXT    NOT NULL DEFAULT '',
    description   TEXT    NOT NULL DEFAULT '',
    display_name  TEXT    NOT NULL DEFAULT '',
    model_id      TEXT,
    persona       TEXT,
    config_json   TEXT,
    is_builtin    INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0, 1)),
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_chat_agent_template_builtin
    ON chat_agent_template(is_builtin);
