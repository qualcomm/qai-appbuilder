-- ============================================================================
-- Migration 003: app_builder context schema (PR-022)
--
-- Creates 7 tables (schema doc qai-db-schema.md §3.1 ~ §3.7):
--   app_builder_model_definition, app_builder_run, app_builder_artifact,
--   app_builder_share, app_builder_voice_pref, app_builder_audit_entry,
--   app_builder_import_commit
--
-- Cross-context: required_catalog_ids_json holds opaque catalog ids as a
-- soft reference to model_catalog (no SQLite FK across contexts;
-- schema doc §0.4 + §9.14).
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- app_builder_model_definition: registered App Builder models (replaces
-- config/app_builder_models.json). id matches AppModelId regex (enforced at
-- the domain layer). pinned/enabled are boolean ints.
-- input_presets_json is a tuple of InputPreset; required_catalog_ids_json is
-- a tuple of opaque model_catalog entry ids (soft reference).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_builder_model_definition (
    id                          TEXT    NOT NULL PRIMARY KEY,
    title                       TEXT    NOT NULL CHECK (length(title) >= 1 AND length(title) <= 200),
    taxonomy_path               TEXT    NOT NULL DEFAULT '',
    enabled                     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    pinned                      INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    input_presets_json          TEXT    NOT NULL DEFAULT '[]',
    required_catalog_ids_json   TEXT    NOT NULL DEFAULT '[]',
    created_at                  TEXT    NOT NULL,
    updated_at                  TEXT    NOT NULL
);

-- UI sort: pinned first, then alphabetical title
CREATE INDEX IF NOT EXISTS ix_app_builder_model_definition_pinned_title
    ON app_builder_model_definition(pinned DESC, title);


-- ----------------------------------------------------------------------------
-- app_builder_run: one execution of an AppModelDefinition (replaces
-- data/app_builder_runs.db). status literals match domain RunStatus
-- (6 states: pending / running / streaming / completed / failed / cancelled).
-- The "status='failed' implies error_message NOT NULL" cross-column rule is
-- enforced by the domain (Run.__post_init__) — kept out of CHECK to avoid
-- composing CHECK constraints (schema doc §9.11).
-- inputs_json holds the dict[str, object] inputs payload.
-- ON DELETE RESTRICT for model_id: cannot delete a model that has runs
-- (audit / history must be preserved).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_builder_run (
    id              TEXT NOT NULL PRIMARY KEY,
    model_id        TEXT NOT NULL,
    inputs_json     TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'running', 'streaming',
                                           'completed', 'failed', 'cancelled')),
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    error_message   TEXT,
    FOREIGN KEY (model_id) REFERENCES app_builder_model_definition(id) ON DELETE RESTRICT
);

-- FK index + list-runs-by-model + active-runs hot path + status filter
CREATE INDEX IF NOT EXISTS ix_app_builder_run_model_created
    ON app_builder_run(model_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_app_builder_run_active
    ON app_builder_run(created_at DESC)
    WHERE status IN ('pending', 'running', 'streaming');
CREATE INDEX IF NOT EXISTS ix_app_builder_run_status_created
    ON app_builder_run(status, created_at DESC);


-- ----------------------------------------------------------------------------
-- app_builder_artifact: a single file produced by a Run.
-- kind literals match domain ArtifactKind (audio/image/text/binary).
-- relative_path validation (no abs / no '..' / no 'data/' prefix) is enforced
-- by the domain VO; we only enforce length here.
-- checksum_sha256 is optional (schema doc §3.3); when set, must be 64 hex chars.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_builder_artifact (
    id                TEXT    NOT NULL PRIMARY KEY,
    run_id            TEXT    NOT NULL,
    relative_path     TEXT    NOT NULL CHECK (length(relative_path) <= 1024),
    size_bytes        INTEGER NOT NULL CHECK (size_bytes >= 0),
    kind              TEXT    NOT NULL CHECK (kind IN ('audio', 'image', 'text', 'binary')),
    checksum_sha256   TEXT             CHECK (checksum_sha256 IS NULL OR length(checksum_sha256) = 64),
    created_at        TEXT    NOT NULL,
    FOREIGN KEY (run_id) REFERENCES app_builder_run(id) ON DELETE CASCADE
);

-- FK index + chronological listing per run
CREATE INDEX IF NOT EXISTS ix_app_builder_artifact_run
    ON app_builder_artifact(run_id, created_at);


-- ----------------------------------------------------------------------------
-- app_builder_share: shareable token referencing a run (replaces
-- data/app_builder_share.db). revoked is a boolean flag; revoking soft-deletes.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_builder_share (
    id          TEXT    NOT NULL PRIMARY KEY,
    run_id      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    FOREIGN KEY (run_id) REFERENCES app_builder_run(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_app_builder_share_run
    ON app_builder_share(run_id);
-- Active (non-revoked) shares hot path — token lookup
CREATE INDEX IF NOT EXISTS ix_app_builder_share_active
    ON app_builder_share(id)
    WHERE revoked = 0;


-- ----------------------------------------------------------------------------
-- app_builder_voice_pref: singleton preference row (replaces
-- data/app_builder/voice_input_pref.json, 57 B). preferred_model_id is a
-- soft reference to app_builder_model_definition.id (NULL = let adapter pick
-- a default). Future multi-user lift will add a user_id column + UNIQUE
-- (schema doc §10.6 decision).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_builder_voice_pref (
    id                   TEXT    NOT NULL PRIMARY KEY DEFAULT 'default',
    enabled              INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    preferred_model_id   TEXT,                                     -- soft FK; nullable
    updated_at           TEXT    NOT NULL
);


-- ----------------------------------------------------------------------------
-- app_builder_audit_entry: append-only audit log (replaces
-- data/app_builder_audit.jsonl, 58 KB). event_type literals defined by
-- schema doc §3.6 (denormalised event taxonomy — distinct from the
-- fully-qualified event_type names in qai.app_builder.domain.events; this
-- table stores short labels for quick filtering / reporting).
-- run_id and model_id are soft references (audit must outlive deletion;
-- schema doc §3.6 explicitly nullable).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_builder_audit_entry (
    id            TEXT NOT NULL PRIMARY KEY,
    run_id        TEXT,                                            -- soft FK
    model_id      TEXT,                                            -- soft FK
    event_type    TEXT NOT NULL CHECK (event_type IN (
                      'run_started',
                      'run_completed',
                      'run_failed',
                      'run_cancelled',
                      'artifact_created',
                      'import_committed',
                      'import_rolled_back'
                  )),
    occurred_at   TEXT NOT NULL,
    payload_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_app_builder_audit_entry_time
    ON app_builder_audit_entry(occurred_at DESC);
-- Partial: filter-by-run when run_id present (skips dangling import entries)
CREATE INDEX IF NOT EXISTS ix_app_builder_audit_entry_run
    ON app_builder_audit_entry(run_id)
    WHERE run_id IS NOT NULL;


-- ----------------------------------------------------------------------------
-- app_builder_import_commit: the import three-state workflow's commit log
-- (PR-022 manifest §3.3). plan_json holds the full ImportPlan serialisation.
-- A NULL rolled_back_at means the commit is still in effect; setting it makes
-- the rollback idempotent (schema doc §3.7).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_builder_import_commit (
    id                  TEXT NOT NULL PRIMARY KEY,
    created_at          TEXT NOT NULL,
    plan_json           TEXT NOT NULL,
    rolled_back_at      TEXT,                                      -- NULL = still active
    rolled_back_reason  TEXT
);

-- Quick lookup of commits not yet rolled back
CREATE INDEX IF NOT EXISTS ix_app_builder_import_commit_active
    ON app_builder_import_commit(created_at DESC)
    WHERE rolled_back_at IS NULL;
