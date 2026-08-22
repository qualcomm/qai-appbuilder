-- ============================================================================
-- Migration 014: security_policy_rule.op column (V1 4-list parity)
--
-- Adds the operation dimension to policy rules so check_permission can match
-- a rule against the *operation* the caller requested, not just the path
-- glob. Restores the V1 PolicyCenter 4-list taxonomy as an explicit per-rule
-- value (V1 read_allow / write_allow /
-- exec_allow_cwd / exec_deny_patterns):
--
--   read       -> V1 read_allow
--   write      -> V1 write_allow
--   exec       -> V1 exec_allow_cwd
--   exec_deny  -> V1 exec_deny_patterns (regex hard-deny, first gate)
--   any        -> operation-agnostic (default; backward compatible)
--
-- Tail-appended NULLABLE-with-DEFAULT column: every rule written before this
-- migration reads back as 'any' and keeps matching on path glob regardless of
-- the requested operation, exactly as before (zero behaviour change for
-- existing policies).
--
-- Done as a standalone ALTER migration (NOT by editing 001) so databases that
-- already applied 001 are upgraded in-place; the migration runner applies each
-- versioned file exactly once.
--
-- NOTE on uniqueness: the original 001 UNIQUE (policy_id, scope, pattern,
-- case_sensitive) constraint cannot be widened to include ``op`` via ALTER in
-- SQLite without a table rebuild. The domain Policy.__post_init__ already
-- enforces the (scope, pattern, case_sensitive, op) uniqueness invariant at
-- the aggregate level, and the repository fully replaces all rows in one
-- transaction on every save, so the DB-level constraint staying at its
-- original (op-less) shape is harmless: two rules differing only by ``op``
-- still differ in their PRIMARY KEY ``id`` and in ``position``, and the
-- original UNIQUE would only fire on an exact (scope, pattern, case_sensitive)
-- collision — which for distinct ops is a legitimate V1 configuration
-- (e.g. read_allow + exec_allow_cwd on the same path). To allow that, we
-- rebuild the table widening the UNIQUE to include ``op``.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


-- 1) Add the new column with a backward-compatible default.
ALTER TABLE security_policy_rule
    ADD COLUMN op TEXT NOT NULL DEFAULT 'any'
        CHECK (op IN ('read', 'write', 'exec', 'exec_deny', 'any'));


-- 2) Rebuild the table so the (scope, pattern, case_sensitive) UNIQUE is
--    widened to include ``op`` (lets read_allow + exec_allow_cwd coexist on
--    the same path, a legitimate V1 config). SQLite cannot ALTER a UNIQUE
--    constraint in place, so we do the standard 12-step table rebuild
--    (https://www.sqlite.org/lang_altertable.html#otheralter).
CREATE TABLE security_policy_rule__new (
    id              TEXT    NOT NULL PRIMARY KEY,
    policy_id       TEXT    NOT NULL DEFAULT 'singleton',
    scope           TEXT    NOT NULL CHECK (scope IN ('user', 'preset', 'path')),
    pattern         TEXT    NOT NULL CHECK (length(pattern) <= 4096),
    case_sensitive  INTEGER NOT NULL DEFAULT 0 CHECK (case_sensitive IN (0, 1)),
    action          TEXT    NOT NULL CHECK (action IN ('allow', 'deny')),
    description     TEXT    NOT NULL DEFAULT '' CHECK (length(description) <= 1024),
    position        INTEGER NOT NULL,
    op              TEXT    NOT NULL DEFAULT 'any'
                            CHECK (op IN ('read', 'write', 'exec', 'exec_deny', 'any')),
    UNIQUE (policy_id, id),
    UNIQUE (policy_id, scope, pattern, case_sensitive, op),
    FOREIGN KEY (policy_id) REFERENCES security_policy(id) ON DELETE CASCADE
);

INSERT INTO security_policy_rule__new
    (id, policy_id, scope, pattern, case_sensitive, action, description, position, op)
SELECT id, policy_id, scope, pattern, case_sensitive, action, description, position, op
FROM security_policy_rule;

DROP TABLE security_policy_rule;

ALTER TABLE security_policy_rule__new RENAME TO security_policy_rule;

-- Recreate the indexes dropped with the old table.
CREATE INDEX IF NOT EXISTS ix_security_policy_rule_policy_id
    ON security_policy_rule(policy_id);
CREATE INDEX IF NOT EXISTS ix_security_policy_rule_policy_position
    ON security_policy_rule(policy_id, position);
