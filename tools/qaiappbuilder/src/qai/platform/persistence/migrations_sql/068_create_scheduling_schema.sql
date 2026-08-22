-- ============================================================================
-- Migration 068: Scheduling job store for the agent-facing scheduled-task tool
--
-- Adds ``scheduling_job`` — a standalone, multi-row aggregate backing the
-- ``scheduled_task`` tool. Each row is one scheduled task: a self-contained
-- prompt fired on a schedule (interval / one-shot / cron), running as one
-- isolated agent turn with its result delivered back to a target conversation.
--
-- Columns:
--   * ``id``               — stable task identity (also the tool-facing handle).
--   * ``name``             — optional human label; empty string when unset.
--   * ``prompt``           — the self-contained task instruction.
--   * ``schedule_kind``    — 'once' | 'interval' | 'cron'.
--   * ``schedule_display`` — the original human schedule text (UI / echoes).
--   * ``run_at``           — ISO-8601 UTC fire instant for a one-shot; NULL else.
--   * ``interval_seconds`` — positive interval for an interval task; NULL else.
--   * ``cron_expr``        — 5-field cron expression for a cron task; NULL else.
--   * ``conversation_id`` / ``tab_id`` — delivery target (where the result is
--     folded back in). A dedicated, tool-supplied pair, not a live user turn.
--   * ``repeat_times``     — cap on successful fires (NULL = unbounded).
--   * ``completed_runs``   — successful fire count (NOT NULL DEFAULT 0).
--   * ``enabled``          — 1/0; a paused task keeps its row but is skipped.
--   * ``state``            — 'scheduled' | 'paused' | 'completed' | 'error'.
--   * ``created_at``       — ISO-8601 UTC creation time.
--   * ``next_run_at``      — ISO-8601 UTC of the next due fire; NULL = no more.
--   * ``last_run_at``      — ISO-8601 UTC of the last fire; NULL if never run.
--   * ``last_status`` / ``last_error`` — last outcome (empty string when none).
--   * ``version``          — optimistic-lock CAS counter (NOT NULL DEFAULT 0).
--
-- Indexes:
--   * ``idx_scheduling_job_due`` on (enabled, next_run_at) — the tick's
--     "what is due now?" scan filters enabled rows ordered by next_run_at.
--   * ``idx_scheduling_job_conversation`` on (conversation_id) — the tool's
--     per-conversation ``list`` action.
--
-- Timezone: all timestamps are stored as ISO-8601 UTC strings written by the
-- domain Clock (SQLite CURRENT_TIMESTAMP is intentionally avoided so the
-- application clock stays authoritative — mirrors migration 052).
--
-- Additive only (AGENTS.md §8): a brand-new table, no existing table / index /
-- trigger touched. Idempotence: the migration runner records applied ids in
-- ``_qai_schema_migrations`` and skips already-applied files. Standalone
-- migration — existing databases upgrade in-place. The runner manages
-- BEGIN/COMMIT; this file MUST NOT contain transaction statements.
-- ============================================================================

CREATE TABLE IF NOT EXISTS scheduling_job (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL DEFAULT '',
    prompt            TEXT NOT NULL,
    schedule_kind     TEXT NOT NULL,
    schedule_display  TEXT NOT NULL,
    run_at            TEXT,
    interval_seconds  REAL,
    cron_expr         TEXT,
    conversation_id   TEXT NOT NULL,
    tab_id            TEXT NOT NULL,
    repeat_times      INTEGER,
    completed_runs    INTEGER NOT NULL DEFAULT 0,
    enabled           INTEGER NOT NULL DEFAULT 1,
    state             TEXT NOT NULL DEFAULT 'scheduled',
    created_at        TEXT NOT NULL,
    next_run_at       TEXT,
    last_run_at       TEXT,
    last_status       TEXT NOT NULL DEFAULT '',
    last_error        TEXT NOT NULL DEFAULT '',
    version           INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_scheduling_job_due
    ON scheduling_job (enabled, next_run_at);

CREATE INDEX IF NOT EXISTS idx_scheduling_job_conversation
    ON scheduling_job (conversation_id);
