-- ============================================================================
-- 061_pending_permission_conversation_gone.sql
--
-- Review fix (2026-07-28) for M-Sec-3: the PendingCleanupService now emits a
-- 'conversation_gone' resolution (process alive but its conversation was
-- terminated / detached — see pending_cleanup._sweep_once). Migration 052's
-- CHECK constraint only permitted {allow, deny, user_cancelled,
-- subprocess_gone, shutdown}, so a 'conversation_gone' row would violate the
-- constraint (or be coerced to 'deny' at the store layer, defeating the whole
-- point of distinguishing the cause).
--
-- SQLite cannot ALTER an existing CHECK constraint, so we rebuild the table
-- following the documented table-redefinition recipe (create new, copy rows,
-- drop old, rename). This is ADDITIVE per red-line §8: it only WIDENS the
-- allowed resolution set (adds one value), preserves every existing row
-- verbatim, and re-creates both indexes.
--
-- Idempotence: the migration runner records applied ids in
-- ``_qai_schema_migrations`` and applies each versioned file EXACTLY ONCE
-- (see qai.platform.persistence.migrations.MigrationRunner), so this
-- non-reentrant DROP/RENAME never replays. Migration 052 unconditionally
-- CREATE TABLE IF NOT EXISTS'd the base table for every DB, so the source
-- table is guaranteed present here.

CREATE TABLE IF NOT EXISTS security_pending_permission_v061 (
    request_id       TEXT PRIMARY KEY,
    pid              INTEGER NOT NULL,
    process_path     TEXT,
    command_line     TEXT,
    path             TEXT NOT NULL,
    event            INTEGER NOT NULL,
    boot_id          TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    resolved_at      TEXT,
    resolution       TEXT,
    actor_parent_pid INTEGER,
    CHECK (
        resolution IS NULL OR resolution IN (
            'allow', 'deny', 'user_cancelled', 'subprocess_gone',
            'shutdown', 'conversation_gone'
        )
    ),
    CHECK (
        (resolved_at IS NULL AND resolution IS NULL)
        OR (resolved_at IS NOT NULL AND resolution IS NOT NULL)
    )
);

-- Copy any existing rows across (idempotent: INSERT OR IGNORE on the PK so a
-- partial prior run does not duplicate). When the source table does not exist
-- yet (fresh DB where 052 ran and created the base table) this still copies 0
-- rows harmlessly.
INSERT OR IGNORE INTO security_pending_permission_v061
    (request_id, pid, process_path, command_line, path, event, boot_id,
     created_at, resolved_at, resolution, actor_parent_pid)
SELECT request_id, pid, process_path, command_line, path, event, boot_id,
       created_at, resolved_at, resolution, actor_parent_pid
FROM security_pending_permission;

DROP TABLE security_pending_permission;

ALTER TABLE security_pending_permission_v061
    RENAME TO security_pending_permission;

-- Re-create both indexes (dropped with the old table).
CREATE INDEX IF NOT EXISTS idx_pending_permission_pid
    ON security_pending_permission (pid)
    WHERE resolved_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_pending_permission_dedupe
    ON security_pending_permission (pid, path, event)
    WHERE resolved_at IS NULL;
