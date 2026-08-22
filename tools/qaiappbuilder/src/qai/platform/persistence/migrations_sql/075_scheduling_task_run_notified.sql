-- ============================================================================
-- Migration 076: track notification read state on each scheduled-task run.
--
-- Root-cause fix for "scheduled-task result did not surface in the notification
-- bell" reports. The previous design pushed a WebSocket event on every fire
-- and relied on that event as the SOLE transport into the bell list. Two
-- failure modes made this fragile:
--
--   1. WebSocket produce-and-consume race — an event fired while the client
--      briefly disconnected (network blip, tab-suspend, Windows
--      ProactorEventLoop silent-dead sockets, …) was consumed by zero
--      subscribers and lost forever (no server-side buffering).
--   2. Cross-boundary drift — the run history table already persisted every
--      fire's full result, but the bell drew only from live WS events. Two
--      systems projected the same fact from two independent transports; on
--      any WS gap the two views diverged and the bell went silent while the
--      history panel showed the run.
--
-- Fix: promote the run-history table to the single source of truth for
-- notifications. A row's ``notified_at`` state:
--
--   * empty string (``''``) — UNREAD; the row is a pending bell item that
--     ``GET /api/scheduled-tasks/notifications/unread`` returns. Every fire
--     inserts with this default via the existing ``TaskRunRecord`` writer, so
--     no producer-side change is needed to enrol new fires.
--   * ISO-8601 UTC timestamp — READ; the moment the user (or the "mark all
--     read" bulk action) dismissed it. Once stamped the row is no longer
--     surfaced as a notification, but the run-history panel still shows it
--     (history is time-ordered, not filtered by read state).
--
-- Additive-only (AGENTS.md schema forward-compat): ONE nullable column with
-- an empty-string default; every pre-076 row auto-reads as UNREAD, which
-- means an install with a backlog of un-notified fires will surface them the
-- next time the WebUI opens (best-effort backfill of history the user may
-- have missed while the WS was down). If that backlog is unwanted an
-- operator can seed ``notified_at = created_at`` for pre-076 rows, but
-- migrations do not do that automatically because "silently swallow every
-- historical run" is a worse default than "show them once".
-- ============================================================================

ALTER TABLE scheduling_task_run
    ADD COLUMN notified_at TEXT NOT NULL DEFAULT '';

-- Partial index over the unread set — the notifications endpoint hits this
-- with ``WHERE notified_at = ''`` on every WebUI open + WS reconnect, so an
-- index tightens the read path even when the total run history grows large.
-- Newest-first for direct pagination in the endpoint.
CREATE INDEX IF NOT EXISTS idx_scheduling_task_run_unread
    ON scheduling_task_run (ran_at DESC)
    WHERE notified_at = '';
