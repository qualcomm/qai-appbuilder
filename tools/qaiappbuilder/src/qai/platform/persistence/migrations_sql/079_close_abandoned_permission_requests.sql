-- ============================================================================
-- 079_close_abandoned_permission_requests.sql
--
-- Bugfix (2026-08-09): close the ghost 'pending' rows left in
-- security_permission_request by ASKs that were already resolved elsewhere.
--
-- 编号说明：本文件原为 078，与 077_..._system_notice_index 一起后移一位，因为
-- 077_create_data_migrations_table（随 PR #267 进入 main）占用了 077。已发布的
-- 迁移不能改名 —— Migration.id 是版本号+名字，改名会让它在所有已升级的库上重跑。
-- 改名后本迁移会以新 id 重跑一次；三条 UPDATE 都以 state='pending' 收敛，已改为
-- 'rejected' 的行不再匹配，故为 no-op。
--
-- The defect
-- ----------
-- PendingCleanupService._sweep_once() resolves an ASK whose subprocess died
-- before the user answered: it wakes the waiting future, closes the UI dialog
-- (PermissionResolvedEvent) and marks the durable mirror row
-- (security_pending_permission.resolution = 'subprocess_gone').  It did NOT
-- touch the security_permission_request aggregate -- and THAT is the table the
-- UI's "pending requests" list reads.  So every swept ASK left a row stuck at
-- state='pending' / resolved_at=NULL forever, even though its dialog had
-- already closed and its waiter was long gone.
--
-- The canonical producer is onnxruntime: SetupAPI rewrites the precompiled-INF
-- cache (C:\Windows\INF\*.PNF) during ordinary CPU / device enumeration, which
-- a bare `import onnxruntime; get_available_providers()` triggers.  The writer
-- does not wait for the verdict (a failed PNF cache write is non-fatal), so the
-- subprocess exits within seconds, the sweep resolves the ASK -- and one more
-- ghost row accumulates.  Each inference run added several.
--
-- The code fixes shipped alongside this migration:
--   * pending_cleanup.py  -- the sweep now closes the aggregate too, and marks
--     the durable mirror BEFORE waking the waiter so the real reason
--     ('subprocess_gone' / 'conversation_gone') is no longer overwritten by
--     the native bridge's generic 'deny' (mark_resolved is first-write-wins).
--   * _native_hook_bridge.py -- the native ASK path emits the
--     filesec.ask_created / filesec.ask_resolved pair it previously lacked
--     entirely, so this class of defect is visible in the log next time.
--   * factory/config/file_guard_paths.json -- %SystemRoot%\INF gets
--     write='deny' (a SILENT hard-deny), so the unanswerable prompt is not
--     raised in the first place.  The write is refused exactly as before; it
--     was never approved.
--
-- What this migration does
-- ------------------------
-- Data-only, idempotent backfill for databases that already accumulated the
-- ghosts (a fresh install has none).  Two disjoint classes, both provably
-- abandoned -- neither can have a live waiter, because a waiter only exists
-- inside the process that created it and both classes pre-date this boot:
--
--   (a) The durable mirror says the ASK is already resolved.  The dialog was
--       closed and the native waiter woken at that moment; the aggregate is a
--       pure leftover projection.  resolution_reason carries the mirror's own
--       reason, so a swept row stays distinguishable from a user rejection.
--
--   (b) A native ASK (subject_identifier = 'native.file_guard') with NO mirror
--       row at all.  The mirror is written on ASK-open by the same bridge that
--       creates the aggregate, so a missing mirror means the row predates the
--       durable-pending store or its insert failed; either way the process that
--       held the waiter is gone.  Scoped to native rows only: an in-process
--       ai_coding.tool ASK never writes a mirror row and could still be live.
--
-- state='rejected' (not 'cancelled'): the waiter really did receive a DENY --
-- the sweep resolves with allow=False -- and 'rejected' is the transition that
-- carries a reason.  Migration 001 CHECK-constrains state to
-- {pending, approved, rejected, expired, cancelled}, so the machine-readable
-- reason cannot live in state; it goes to resolution_reason.
--
-- Red-line §8 compliance: data-only (no schema change), additive, idempotent
-- (the WHERE clauses exclude anything already resolved, so a re-run is a
-- no-op), and it never touches a row that could still have a live waiter.
-- The runner manages BEGIN/COMMIT -- this file must not contain them.
-- ============================================================================

-- (a) Aggregate still 'pending' while the durable mirror is already resolved.
UPDATE security_permission_request
SET state             = 'rejected',
    resolved_at       = COALESCE(
                            (SELECT p.resolved_at
                               FROM security_pending_permission p
                              WHERE p.request_id = security_permission_request.id),
                            created_at
                        ),
    resolution_reason = COALESCE(
                            (SELECT p.resolution
                               FROM security_pending_permission p
                              WHERE p.request_id = security_permission_request.id),
                            'abandoned'
                        )
WHERE state = 'pending'
  AND EXISTS (
        SELECT 1
          FROM security_pending_permission p
         WHERE p.request_id = security_permission_request.id
           AND p.resolved_at IS NOT NULL
      );

-- (b) Native ASK with no durable mirror row at all -- the owning process is
--     gone, so nothing can ever resolve it.  In-process (ai_coding.tool) rows
--     are deliberately left alone: they never write a mirror row and a live
--     dialog would be indistinguishable from an abandoned one here.
UPDATE security_permission_request
SET state             = 'rejected',
    resolved_at       = created_at,
    resolution_reason = 'abandoned'
WHERE state = 'pending'
  AND subject_identifier = 'native.file_guard'
  AND NOT EXISTS (
        SELECT 1
          FROM security_pending_permission p
         WHERE p.request_id = security_permission_request.id
      );

-- (c) Any remaining 'pending' aggregate that predates THIS migration run by
--     more than a day.  A permission request only has a waiter inside the
--     process that minted it: the in-memory PermissionWaitRegistry future
--     dies with the process, and nothing rehydrates an in-process
--     (ai_coding.tool) waiter across a restart -- there is no expire()/TTL
--     caller anywhere in the codebase (verified 2026-08-09), so these rows
--     were immortal.  A day is far beyond any dialog a human leaves open
--     (the WaitWatchdogService already WARNs at 15 minutes) while staying
--     comfortably clear of a genuinely live prompt: the migration runs at
--     startup, before any ASK of this boot can exist, so a row younger than
--     a day can only belong to a previous process -- the cutoff is pure
--     belt-and-braces.
--
--     resolution_reason='abandoned_stale' keeps this class separately
--     auditable from (a)/(b) and from a genuine user rejection.
--
--     NOTE on the comparison: created_at is ISO-8601 with a 'T' separator and
--     an offset ('2026-08-06T00:42:27.588916+00:00'), whereas datetime() emits
--     'YYYY-MM-DD HH:MM:SS' with a SPACE.  Comparing those as raw strings is
--     wrong -- at position 11 it pits 'T' (0x54) against ' ' (0x20), so a row
--     one second older than the cutoff still compares GREATER and escapes the
--     sweep.  Both sides therefore go through strftime() into one canonical
--     format before comparing (verified against a same-second boundary probe).
UPDATE security_permission_request
SET state             = 'rejected',
    resolved_at       = created_at,
    resolution_reason = 'abandoned_stale'
WHERE state = 'pending'
  AND strftime('%Y-%m-%dT%H:%M:%S', created_at)
      < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 day');
