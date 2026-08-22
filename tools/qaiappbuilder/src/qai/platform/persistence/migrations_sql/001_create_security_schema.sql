-- ============================================================================
-- Migration 001: security context schema (PR-020)
--
-- Creates 6 tables (schema doc qai-db-schema.md §1.1 ~ §1.6):
--   security_policy, security_policy_rule, security_sandbox_grant,
--   security_acl_tracking, security_permission_request, security_audit_entry
--
-- Note: §1.7 security_policy_template is NOT persisted in DB (ST-class
-- file under config/policy_templates/*.json — schema doc §1.7 decision).
--
-- Conventions (schema doc §0.3 / §0.4 / §9):
--   * id TEXT PRIMARY KEY (ULID/UUIDv7 string)
--   * timestamps TEXT NOT NULL ISO 8601 UTC
--   * boolean = INTEGER CHECK IN (0, 1)
--   * AceMask = INTEGER CHECK BETWEEN 1 AND 15 (R=1 W=2 E=4 D=8;
--     mask=0 disallowed because empty grants make no sense)
--   * cross-context: NO foreign keys (only TEXT soft refs)
--   * runner manages BEGIN/COMMIT — this file MUST NOT contain them
-- ============================================================================


-- ----------------------------------------------------------------------------
-- security_policy: singleton aggregate root holding the global policy version.
-- One row only (id='singleton'). Bumped on every rule change so the reboot
-- decision (REBOOT_EXIT_CODE=75, plan §8.11) can detect deltas.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS security_policy (
    id          TEXT    NOT NULL PRIMARY KEY,
    version     INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at  TEXT    NOT NULL
);


-- ----------------------------------------------------------------------------
-- security_policy_rule: ordered set of rules belonging to the singleton policy.
-- scope/action literals match domain Enum.value (PolicyScope/PolicyAction).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS security_policy_rule (
    id              TEXT    NOT NULL PRIMARY KEY,
    policy_id       TEXT    NOT NULL DEFAULT 'singleton',
    scope           TEXT    NOT NULL CHECK (scope IN ('user', 'preset', 'path')),
    pattern         TEXT    NOT NULL CHECK (length(pattern) <= 4096),
    case_sensitive  INTEGER NOT NULL DEFAULT 0 CHECK (case_sensitive IN (0, 1)),
    action          TEXT    NOT NULL CHECK (action IN ('allow', 'deny')),
    description     TEXT    NOT NULL DEFAULT '' CHECK (length(description) <= 1024),
    position        INTEGER NOT NULL,
    -- rule_id unique within the policy (id alone is the PK; this enforces the
    -- documented composite invariant from schema doc §1.2)
    UNIQUE (policy_id, id),
    -- (scope, pattern, case_sensitive) must be unique per policy
    UNIQUE (policy_id, scope, pattern, case_sensitive),
    FOREIGN KEY (policy_id) REFERENCES security_policy(id) ON DELETE CASCADE
);

-- FK index (schema doc §9.6) + ordered traversal index for evaluate()
CREATE INDEX IF NOT EXISTS ix_security_policy_rule_policy_id
    ON security_policy_rule(policy_id);
CREATE INDEX IF NOT EXISTS ix_security_policy_rule_policy_position
    ON security_policy_rule(policy_id, position);


-- ----------------------------------------------------------------------------
-- security_sandbox_grant: persistent ACL entries (replaces config/persistent_acl.json,
-- 89.7 KB / 7800+ rows). subject_kind matches domain Subject._ALLOWED_KINDS;
-- source matches domain GrantSource. mask_bits >= 1: empty grants rejected.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS security_sandbox_grant (
    id                  TEXT    NOT NULL PRIMARY KEY,
    subject_kind        TEXT    NOT NULL CHECK (subject_kind IN ('user', 'preset', 'system')),
    subject_identifier  TEXT    NOT NULL CHECK (length(subject_identifier) <= 512),
    path                TEXT    NOT NULL CHECK (length(path) <= 4096),
    mask_bits           INTEGER NOT NULL CHECK (mask_bits BETWEEN 1 AND 15),
    source              TEXT    NOT NULL CHECK (source IN ('user', 'auto', 'preset')),
    created_at          TEXT    NOT NULL,
    expires_at          TEXT             -- NULL = no expiry
);

-- Hot-path indexes (schema doc §1.3)
CREATE INDEX IF NOT EXISTS ix_security_sandbox_grant_subject
    ON security_sandbox_grant(subject_kind, subject_identifier, path);
CREATE INDEX IF NOT EXISTS ix_security_sandbox_grant_path
    ON security_sandbox_grant(path);
-- Partial index for "long-lived grants" lookup
CREATE INDEX IF NOT EXISTS ix_security_sandbox_grant_unexpired
    ON security_sandbox_grant(subject_kind, subject_identifier)
    WHERE expires_at IS NULL;


-- ----------------------------------------------------------------------------
-- security_acl_tracking: append-only audit trail of grant lifecycle events
-- (replaces data/persistent_acl_tracking.txt). event_type values defined by
-- schema doc §1.4 (no direct domain Enum — these are tracking-only labels).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS security_acl_tracking (
    id           TEXT NOT NULL PRIMARY KEY,
    grant_id     TEXT NOT NULL,
    event_type   TEXT NOT NULL CHECK (event_type IN ('add', 'remove', 'revoke')),
    occurred_at  TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 1024),
    FOREIGN KEY (grant_id) REFERENCES security_sandbox_grant(id) ON DELETE CASCADE
);

-- FK index + recent-events-by-grant query
CREATE INDEX IF NOT EXISTS ix_security_acl_tracking_grant_id
    ON security_acl_tracking(grant_id);
CREATE INDEX IF NOT EXISTS ix_security_acl_tracking_grant_time
    ON security_acl_tracking(grant_id, occurred_at DESC);


-- ----------------------------------------------------------------------------
-- security_permission_request: in-flight or resolved approval workflow items.
-- state values match domain RequestState; resource_kind matches domain
-- Resource._ALLOWED_KINDS; subject_kind matches Subject._ALLOWED_KINDS.
-- requested_mask_bits >= 1 mirrors AceMask domain invariant (must be non-empty).
-- The "resolved_at NULL when state=pending" cross-column rule is enforced by
-- the application layer (PermissionRequest.__post_init__), kept out of CHECK
-- to avoid CHECK complexity (schema doc §9.11).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS security_permission_request (
    id                    TEXT    NOT NULL PRIMARY KEY,
    subject_kind          TEXT    NOT NULL CHECK (subject_kind IN ('user', 'preset', 'system')),
    subject_identifier    TEXT    NOT NULL,
    resource_kind         TEXT    NOT NULL CHECK (resource_kind IN ('path', 'skill', 'network', 'exec', 'dep')),
    resource_identifier   TEXT    NOT NULL CHECK (length(resource_identifier) <= 4096),
    requested_mask_bits   INTEGER NOT NULL CHECK (requested_mask_bits BETWEEN 1 AND 15),
    state                 TEXT    NOT NULL DEFAULT 'pending'
                                  CHECK (state IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')),
    created_at            TEXT    NOT NULL,
    resolved_at           TEXT,
    resolution_reason     TEXT    NOT NULL DEFAULT '' CHECK (length(resolution_reason) <= 2048)
);

-- list_pending() hot path
CREATE INDEX IF NOT EXISTS ix_security_permission_request_pending
    ON security_permission_request(created_at DESC)
    WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS ix_security_permission_request_subject
    ON security_permission_request(subject_kind, subject_identifier, created_at DESC);


-- ----------------------------------------------------------------------------
-- security_audit_entry: append-only record of security decisions
-- (replaces data/security_audit.jsonl, 136.4 KB). decision matches domain
-- PolicyAction. rule_id is a soft reference (rule may have been deleted).
-- TTL not enforced at schema level — operators DELETE WHERE occurred_at < ?
-- (schema doc §1.6).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS security_audit_entry (
    id                   TEXT NOT NULL PRIMARY KEY,
    occurred_at          TEXT NOT NULL,
    subject_kind         TEXT NOT NULL,
    subject_identifier   TEXT NOT NULL,
    resource_kind        TEXT NOT NULL,
    resource_identifier  TEXT NOT NULL,
    decision             TEXT NOT NULL CHECK (decision IN ('allow', 'deny')),
    rule_id              TEXT,                                    -- soft FK; rule may be deleted
    correlation_id       TEXT          CHECK (correlation_id IS NULL OR length(correlation_id) <= 128),
    note                 TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 2048)
);

-- Hot paths from schema doc §1.6
CREATE INDEX IF NOT EXISTS ix_security_audit_entry_subject_time
    ON security_audit_entry(subject_kind, subject_identifier, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_security_audit_entry_correlation
    ON security_audit_entry(correlation_id)
    WHERE correlation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_security_audit_entry_time
    ON security_audit_entry(occurred_at DESC);
