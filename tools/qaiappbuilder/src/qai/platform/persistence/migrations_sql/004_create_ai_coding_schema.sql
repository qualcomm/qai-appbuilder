-- ============================================================================
-- Migration 004: ai_coding context schema (PR-023)
--
-- Creates 5 tables (schema doc qai-db-schema.md §4.1 ~ §4.5):
--   ai_coding_session, ai_coding_message, ai_coding_permission_request,
--   ai_coding_tool_invocation, ai_coding_skill
--
-- NOTE: ai_coding_skill is DROPPED again by migration 072 (see the block
-- above its CREATE below); only 4 of these tables survive on a current DB.
--
-- Replaces the legacy two-DB split: data/cc_sessions.db + data/oc_sessions.db.
-- The new design unifies both via the ``provider`` discriminator column on
-- ai_coding_session (CHECK IN ('claude_code', 'open_code')).
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- ai_coding_session: aggregate root unifying CC + OC sessions.
-- provider matches domain Provider Enum (claude_code / open_code).
-- status matches domain SessionStatus (6 states: pending / active / idle /
-- streaming / permission_requested / terminated).
-- last_stream_sequence defaults to -1 (matches CodingSession default).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_coding_session (
    id                     TEXT    NOT NULL PRIMARY KEY,
    provider               TEXT    NOT NULL CHECK (provider IN ('claude_code', 'open_code')),
    workspace_path         TEXT    NOT NULL CHECK (length(workspace_path) <= 4096),
    status                 TEXT    NOT NULL DEFAULT 'pending'
                                   CHECK (status IN ('pending', 'active', 'idle',
                                                     'streaming', 'permission_requested',
                                                     'terminated')),
    title                  TEXT,
    last_stream_sequence   INTEGER NOT NULL DEFAULT -1,
    created_at             TEXT    NOT NULL,
    updated_at             TEXT    NOT NULL,
    terminated_at          TEXT,
    termination_reason     TEXT
);

-- list_active() hot path (excludes terminated rows)
CREATE INDEX IF NOT EXISTS ix_ai_coding_session_active
    ON ai_coding_session(updated_at DESC)
    WHERE status != 'terminated';
-- workspace lock reverse lookup
CREATE INDEX IF NOT EXISTS ix_ai_coding_session_workspace
    ON ai_coding_session(workspace_path);
-- per-provider filtering (UI tabs)
CREATE INDEX IF NOT EXISTS ix_ai_coding_session_provider_status
    ON ai_coding_session(provider, status);


-- ----------------------------------------------------------------------------
-- ai_coding_message: ordered user messages within a session.
-- text length capped at 256 KiB to mirror domain MessageContent invariant.
-- position is 0-based; UNIQUE(session_id, position) preserves order.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_coding_message (
    id          TEXT    NOT NULL PRIMARY KEY,
    session_id  TEXT    NOT NULL,
    text        TEXT    NOT NULL CHECK (length(text) >= 1 AND length(text) <= 262144),
    position    INTEGER NOT NULL CHECK (position >= 0),
    created_at  TEXT    NOT NULL,
    UNIQUE (session_id, position),
    FOREIGN KEY (session_id) REFERENCES ai_coding_session(id) ON DELETE CASCADE
);

-- FK index (UNIQUE above already covers session_id-prefix queries, but the
-- explicit single-column index is helpful for joins / cardinality estimates)
CREATE INDEX IF NOT EXISTS ix_ai_coding_message_session_id
    ON ai_coding_message(session_id);


-- ----------------------------------------------------------------------------
-- ai_coding_permission_request: prompts the user must decide before a tool runs.
-- decision matches domain PermissionDecision (pending / approved / rejected).
-- args_json holds the dict[str, Any] arg payload.
-- The "at most one pending request per session" domain invariant is enforced
-- by application layer (CodingSession.request_permission); we add a partial
-- UNIQUE index as a defence-in-depth at the data layer (schema doc §4.3).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_coding_permission_request (
    id            TEXT NOT NULL PRIMARY KEY,
    session_id    TEXT NOT NULL,
    tool_name     TEXT NOT NULL CHECK (length(tool_name) <= 128),
    args_json     TEXT NOT NULL DEFAULT '{}',
    decision      TEXT NOT NULL DEFAULT 'pending'
                       CHECK (decision IN ('pending', 'approved', 'rejected')),
    requested_at  TEXT NOT NULL,
    decided_at    TEXT,
    FOREIGN KEY (session_id) REFERENCES ai_coding_session(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_ai_coding_permission_request_session_id
    ON ai_coding_permission_request(session_id);
-- Partial UNIQUE: at most one pending permission request per session
CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_coding_permission_request_pending
    ON ai_coding_permission_request(session_id)
    WHERE decision = 'pending';


-- ----------------------------------------------------------------------------
-- ai_coding_tool_invocation: record of one tool call.
-- status is a free string in domain (ToolInvocation.status: running/completed/
-- failed) — we add an explicit CHECK at the schema layer to enforce the
-- documented set (schema doc §4.4).
-- duration_ms / result_json / error_code are NULL until the invocation finishes.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_coding_tool_invocation (
    id            TEXT    NOT NULL PRIMARY KEY,
    session_id    TEXT    NOT NULL,
    tool_name     TEXT    NOT NULL CHECK (length(tool_name) <= 128),
    args_json     TEXT    NOT NULL DEFAULT '{}',
    status        TEXT    NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running', 'completed', 'failed')),
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    duration_ms   INTEGER          CHECK (duration_ms IS NULL OR duration_ms >= 0),
    result_json   TEXT,
    error_code    TEXT,
    FOREIGN KEY (session_id) REFERENCES ai_coding_session(id) ON DELETE CASCADE
);

-- FK index + recent invocations per session + partial running-status query
CREATE INDEX IF NOT EXISTS ix_ai_coding_tool_invocation_session_started
    ON ai_coding_tool_invocation(session_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_ai_coding_tool_invocation_running
    ON ai_coding_tool_invocation(session_id)
    WHERE status = 'running';


-- ----------------------------------------------------------------------------
-- ai_coding_skill: registered ai_coding skills (mirror of legacy skill_policy
-- without security coupling). spec_json stores the dict[str, Any] spec payload.
--
-- SUPERSEDED: migration 072 DROPS this table. PR-103 routed the ai_coding
-- skill port through SkillRegistryBridge onto the canonical
-- model_catalog_skill table (§6.6), after which nothing read or wrote this
-- one. The CREATE below is intentionally left intact -- published migrations
-- are never rewritten (AGENTS.md §8.4); a fresh database creates the table
-- here and drops it in 072, while an existing database just drops it.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_coding_skill (
    name           TEXT    NOT NULL PRIMARY KEY,
    description    TEXT    NOT NULL DEFAULT '',
    spec_json      TEXT    NOT NULL DEFAULT '{}',
    enabled        INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    registered_at  TEXT    NOT NULL
);

-- Filter enabled skills (UI registry display)
CREATE INDEX IF NOT EXISTS ix_ai_coding_skill_enabled
    ON ai_coding_skill(name)
    WHERE enabled = 1;
