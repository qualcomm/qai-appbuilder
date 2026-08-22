-- ============================================================================
-- Migration 040: chat_mode_template — collaboration-mode templates
--
-- Creates 1 table (schema doc qai-db-schema.md §2.10):
--   chat_mode_template  — a named COLLABORATION MODE ("怎么协作": 讨论 / 评审 /
--                         辩论 / 实施 / custom) — the third tier of the
--                         three-tier template system (design §26 / §27).
--                         A team answers "谁参与" (chat_roster_template); a mode
--                         answers "怎么协作" (this). They are ORTHOGONAL — the
--                         same team can run any mode; the same mode applies to
--                         any team. PURE V2 enhancement (V1 has no multi-agent
--                         discussion at all).
--
-- A mode is identity + framing + tool_policy + flow_policy (the V1 subset of
-- design §26.1):
--   identity      = id / name / description / is_builtin
--   framing       = framing (the prose that expresses HOW to collaborate; tone +
--                   goal + boundary — it NEVER carries real permission)
--   tool_policy   = tool_policy_json: per-tool allow/deny
--                     {"default": "allow"|"deny",
--                      "tools": {"<tool>": "allow"|"deny", ...}}
--   flow_policy   = flow_policy_json:
--                     {"speaker_strategy":"manager"|"round_robin",
--                      "max_rounds":int, "judge_enabled":bool,
--                      "allow_mode_switch":bool}
--
-- NOTE (V1 scope): execution-time confirmation / sandbox state is intentionally
-- NOT modelled — the discussion runtime executes tools through the ordinary
-- ToolInvocationPort and has no confirmation channel or subprocess sandbox, so
-- carrying require_confirmation / sandbox_required / confirm-before-* would be
-- dead, misleading data (framing promising a confirmation that never happens),
-- violating State-Truth-First. Tool policy is therefore a clean allow/deny. A
-- future version that adds an execution-time gate can extend this.
--
-- JSON blobs (not rigid columns) keep the schema extensible (design §26.7).
--
-- is_builtin (0/1) marks factory-seeded preset modes (seeded by the install
-- pipeline from factory/db_staging/chat_mode_template.jsonl) so the UI can
-- distinguish "preset" from "my saved" modes; built-ins are NOT bound to any
-- conversation (no FK) — they are a global, conversation-independent library.
-- A conversation references its chosen mode via meta["discussion"]["selected_
-- mode_id"] (tail-appended, §3.1), NOT via a FK column here.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================

CREATE TABLE IF NOT EXISTS chat_mode_template (
    id                 TEXT    NOT NULL PRIMARY KEY,
    name               TEXT    NOT NULL DEFAULT '',
    description        TEXT    NOT NULL DEFAULT '',
    framing            TEXT    NOT NULL DEFAULT '',
    tool_policy_json   TEXT    NOT NULL DEFAULT '{}',
    flow_policy_json   TEXT    NOT NULL DEFAULT '{}',
    is_builtin         INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0, 1)),
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_chat_mode_template_builtin
    ON chat_mode_template(is_builtin);
