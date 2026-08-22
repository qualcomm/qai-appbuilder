-- ---------------------------------------------------------------------
-- Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
-- SPDX-License-Identifier: BSD-3-Clause
-- ---------------------------------------------------------------------
--
-- 073_scheduling_task_run_record: per-fire run history for scheduled tasks.
--
-- Every time the scheduler fires a task it appends ONE row here recording the
-- outcome + the FULL result text. This backs three surfaces with one source
-- of truth:
--   * the notification center (open a notification -> read the full result);
--   * the task run-records view (browse every past run);
--   * (implicitly) global tasks, which have no bound conversation to fold
--     their result into — the run record IS their durable output.
--
-- Additive-only (AGENTS.md schema forward-compat): a NEW table + index; no
-- change to existing rows/columns. GLOBAL scheduled tasks are represented on
-- ``scheduling_job`` WITHOUT a schema change — they store an EMPTY-STRING
-- conversation_id / tab_id (the NOT NULL columns from 068 are kept; "global"
-- is the empty-string convention, matching ScheduledTask.is_global which
-- treats a falsy conversation_id/tab_id as global). SQLite cannot drop a
-- NOT NULL constraint without a full table rebuild, so the empty-string
-- convention is the deliberate, non-destructive choice.
--
-- Columns:
--   * id             — ULID/uuid run-record id (PK).
--   * task_id        — the scheduled_job this run belongs to (soft ref; a run
--                      record OUTLIVES its task so history survives deletion).
--   * conversation_id— the conversation the run executed in (the task's bound
--                      conversation, or the global task's dedicated system
--                      conversation); empty string when none.
--   * ok             — 1 success / 0 failure.
--   * status         — 'ok' | 'error' | 'skipped' (mirrors last_status).
--   * result_text    — the run's full assistant text (or failure summary).
--   * ran_at         — ISO-8601 UTC fire time.
--
-- Index: (task_id, ran_at DESC) — the run-records view lists one task's runs
-- newest-first; also serves the "latest run of task X" lookup.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scheduling_task_run (
    id               TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    conversation_id  TEXT NOT NULL DEFAULT '',
    ok               INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT '',
    result_text      TEXT NOT NULL DEFAULT '',
    ran_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheduling_task_run_task
    ON scheduling_task_run (task_id, ran_at DESC);
