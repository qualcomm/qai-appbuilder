-- ============================================================================
-- Migration 055: rename security_sandbox_grant -> security_path_grant
--
-- The OS-isolation sandbox was removed 2026-07-01 (replaced by FileGuard); the
-- persistent-ACL grant aggregate that this table backs is FileGuard's path
-- authorization store and has nothing to do with an OS sandbox anymore. This
-- migration renames the table (and its indexes) to the semantically correct
-- ``security_path_grant`` name. Pure rename: NO column / row / semantics change.
--
-- Foreign key note
-- ----------------
-- ``security_acl_tracking.grant_id`` has ``FOREIGN KEY ... REFERENCES
-- security_sandbox_grant(id) ON DELETE CASCADE`` (migration 001:100). SQLite's
-- ``ALTER TABLE ... RENAME TO`` AUTOMATICALLY rewrites foreign-key references
-- in other tables' schema to point at the new table name (this is the
-- documented SQLite behaviour with ``legacy_alter_table`` OFF, which is the
-- default since SQLite 3.25.0 / the version aiosqlite ships). No manual
-- rebuild of ``security_acl_tracking`` is therefore required, and the
-- ON DELETE CASCADE relationship is preserved byte-for-byte.
--
-- Indexes are NOT auto-renamed by ``RENAME TO`` (only auto-repointed to the
-- new table), so we explicitly drop the old-named indexes and recreate them
-- under the ``ix_security_path_grant_*`` names to keep the naming consistent.
-- Definitions mirror migration 001 (subject / path / unexpired) and migration
-- 051 (scope). Recreating them is a metadata-only, data-preserving operation.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================

ALTER TABLE security_sandbox_grant RENAME TO security_path_grant;

-- Old-named indexes (from migrations 001 + 051). RENAME TO leaves these
-- attached to the renamed table but keeps the stale ``sandbox`` names; drop
-- and recreate under the correct names.
DROP INDEX IF EXISTS ix_security_sandbox_grant_subject;
DROP INDEX IF EXISTS ix_security_sandbox_grant_path;
DROP INDEX IF EXISTS ix_security_sandbox_grant_unexpired;
DROP INDEX IF EXISTS ix_security_sandbox_grant_scope;

-- Recreate (definitions mirror 001:79-86 + 051:42-45, table name updated).
CREATE INDEX IF NOT EXISTS ix_security_path_grant_subject
    ON security_path_grant(subject_kind, subject_identifier, path);
CREATE INDEX IF NOT EXISTS ix_security_path_grant_path
    ON security_path_grant(path);
CREATE INDEX IF NOT EXISTS ix_security_path_grant_unexpired
    ON security_path_grant(subject_kind, subject_identifier)
    WHERE expires_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_security_path_grant_scope
    ON security_path_grant(
        subject_kind, subject_identifier, scope_kind, scope_key
    );
