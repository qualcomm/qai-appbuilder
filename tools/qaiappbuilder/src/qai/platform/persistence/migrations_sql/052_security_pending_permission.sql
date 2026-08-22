-- ============================================================================
-- Migration 052: security_pending_permission (Phase 2 pending-ASK persistence)
--
-- Backs the Phase 2 (2026-07-06) durable-pending contract for FileGuard ASK
-- popups: while a permission request is IN-FLIGHT (a native subprocess file
-- event has hit ASK and is waiting for the user to click approve / reject),
-- we now write a row here so a service restart with unanswered ASKs can:
--
--   1. rehydrate the UI's "pending" list on next boot,
--   2. mark stale rows whose ``boot_id`` != the current boot as ORPHANED so
--      the UI can flag them for user attention (the underlying subprocesses
--      are already dead — the DLL pipe tore down with the old process),
--   3. let :class:`PendingCleanupService` (10s scan interval) resolve rows
--      whose ``pid`` is no longer alive as ``subprocess_gone``.
--
-- This table is deliberately SEPARATE from ``security_permission_request``:
--   * ``security_permission_request`` is the domain aggregate (subject /
--     resource / mask / state); its state machine is
--     pending → approved / rejected / cancelled / expired and it is queried
--     by the domain use cases (approve / reject / cancel_permission_request).
--   * ``security_pending_permission`` is the Phase 2 OPERATIONAL registry
--     mirroring the in-memory ``PermissionWaitRegistry`` — it stores the
--     native-event context (pid, process_path, command_line, event mask,
--     boot_id) that the domain PermissionRequest has no slot for (and is
--     field-locked, so widening it would break §3.1). The two are joined
--     on ``request_id`` when the UI needs the full picture.
--
-- Field notes:
--   * ``event`` — bitfield matching the native ``Event`` enum:
--     1=READ, 2=WRITE, 4=EXECUTE, 8=DELETE.
--   * ``boot_id`` — this backend process's boot id (minted once in lifespan
--     as ``container.boot_id``). Rows whose ``boot_id`` != current are
--     ORPHANED (their DLL pipe is gone; the UI should mark them and let
--     the operator dismiss).
--   * ``created_at`` / ``resolved_at`` — ISO8601 strings (matches the rest
--     of the security schema; SQLite CURRENT_TIMESTAMP is intentionally
--     avoided so the domain Clock is authoritative).
--   * ``resolution`` — 'allow' | 'deny' | 'user_cancelled' | 'subprocess_gone'
--     | 'shutdown'. NULL while the row is still pending. Distinguishes the
--     cause the in-memory :class:`PermissionResolution` cannot carry
--     (allow/deny/timed_out only).
--   * ``actor_parent_pid`` — best-effort parent pid (from FilterEventV2);
--     nullable because the native pipe callback may not always carry it.
--
-- Tail-appended per §3.1 — no existing table / column is renamed or removed.
-- The FastAPI / DI layer treats this table as OPT-IN via
-- ``SecuritySettings.permission_pending_persist`` (default True): a False
-- value wires a null store so tests / in-memory deployments never touch it.
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================

CREATE TABLE IF NOT EXISTS security_pending_permission (
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
            'allow', 'deny', 'user_cancelled', 'subprocess_gone', 'shutdown'
        )
    ),
    CHECK (
        (resolved_at IS NULL AND resolution IS NULL)
        OR (resolved_at IS NOT NULL AND resolution IS NOT NULL)
    )
);

-- Hot-path index for cancel-by-pid + cleanup scans (unresolved rows for a pid).
CREATE INDEX IF NOT EXISTS idx_pending_permission_pid
    ON security_pending_permission (pid)
    WHERE resolved_at IS NULL;

-- Dedupe cross-restart lookup: (pid, path, event) uniquely identifies an
-- ASK triple; when the process restarts we can consult this to warn the
-- operator about "you have unanswered ASKs for this triple from before".
CREATE INDEX IF NOT EXISTS idx_pending_permission_dedupe
    ON security_pending_permission (pid, path, event)
    WHERE resolved_at IS NULL;
