// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Scheduled-task manager store — backs the WebUI panel that lists and edits
 * scheduled tasks (content, schedule, and per-task tool / skill whitelists).
 *
 * Thin Pinia wrapper over the `/api/scheduled-tasks` REST surface (see
 * `interfaces/http/routes/scheduled_tasks.py`). The panel reads `tasks` +
 * `catalog` and calls the actions; every action re-fetches the affected row
 * so the list always reflects persisted server state (State-Truth-First — the
 * backend, not the client, owns task state).
 */
import { defineStore } from "pinia";
import { apiJson } from "@/api";

/** One scheduled task as the REST layer returns it. */
export interface ScheduledTaskItem {
  task_id: string;
  name: string;
  conversation_id: string;
  /** True when the task is global (not bound to any conversation). */
  is_global: boolean;
  /** Bound conversation's title (resolved by the list endpoint; "" if gone). */
  conversation_title: string;
  model_id: string | null;
  prompt: string;
  schedule: string;
  /**
   * First-fire instant for a repeating task (ISO-8601). Lets "every 2h but
   * starting at 09:00" be expressed; null ⇒ first fire is now + interval.
   */
  start_at?: string | null;
  /**
   * The user's local UTC offset in minutes east (e.g. +08:00 ⇒ 480) that the
   * cron wall-clock fields are interpreted in. null ⇒ legacy UTC semantics.
   */
  tz_offset_minutes?: number | null;
  state: string;
  enabled: boolean;
  repeat_times: number | null;
  completed_runs: number;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string;
  last_error: string;
  enabled_tools: string[];
  enabled_skills: string[];
}

interface ListResponse {
  tasks: ScheduledTaskItem[];
  total: number;
}

interface CatalogResponse {
  tools: string[];
  skills: string[];
}

/** One recorded execution of a task (run history + full result text). */
export interface ScheduledTaskRun {
  ok: boolean;
  status: string;
  result_text: string;
  ran_at: string;
}

interface RunsResponse {
  runs: ScheduledTaskRun[];
  total: number;
}

/** Fields an edit may change; only present keys are applied server-side. */
export interface ScheduledTaskUpdate {
  prompt?: string;
  schedule?: string;
  start_at?: string | null;
  tz_offset_minutes?: number | null;
  name?: string;
  repeat_times?: number | null;
  enabled_tools?: string[];
  enabled_skills?: string[];
}

interface State {
  tasks: ScheduledTaskItem[];
  tools: string[];
  skills: string[];
  loading: boolean;
  error: string;
}

const BASE = "/api/scheduled-tasks";

export const useScheduledTasksStore = defineStore("scheduledTasks", {
  state: (): State => ({
    tasks: [],
    tools: [],
    skills: [],
    loading: false,
    error: "",
  }),
  getters: {
    /** Tasks newest-first by next run (nulls last), then by name. */
    sortedTasks: (state): ScheduledTaskItem[] =>
      [...state.tasks].sort((a, b) => {
        const an = a.next_run_at ?? "";
        const bn = b.next_run_at ?? "";
        if (an !== bn) {
          if (an === "") return 1;
          if (bn === "") return -1;
          return an < bn ? -1 : 1;
        }
        return a.name.localeCompare(b.name);
      }),
  },
  actions: {
    /** Load every scheduled task (or one conversation's when scoped). */
    async loadTasks(conversationId?: string): Promise<void> {
      this.loading = true;
      this.error = "";
      try {
        const res = await apiJson<ListResponse>("GET", BASE, undefined, {
          query: conversationId ? { conversation_id: conversationId } : undefined,
        });
        this.tasks = res.tasks;
      } catch (err) {
        this.error = err instanceof Error ? err.message : String(err);
      } finally {
        this.loading = false;
      }
    },

    /** Load the tool + skill whitelist options the editor offers. */
    async loadCatalog(): Promise<void> {
      try {
        const res = await apiJson<CatalogResponse>("GET", `${BASE}/catalog`);
        this.tools = res.tools;
        this.skills = res.skills;
      } catch {
        // Best-effort — the editor still works with an empty catalog (the
        // user just gets no checkbox suggestions).
        this.tools = [];
        this.skills = [];
      }
    },

    /** Replace one task in `tasks` in place (used after every mutation). */
    _upsert(task: ScheduledTaskItem): void {
      const idx = this.tasks.findIndex((t) => t.task_id === task.task_id);
      if (idx >= 0) this.tasks.splice(idx, 1, task);
      else this.tasks = [task, ...this.tasks];
    },

    /** Edit a task's content / schedule / whitelists. Throws on failure. */
    async updateTask(
      taskId: string,
      changes: ScheduledTaskUpdate,
    ): Promise<ScheduledTaskItem> {
      const updated = await apiJson<ScheduledTaskItem, ScheduledTaskUpdate>(
        "PATCH",
        `${BASE}/${encodeURIComponent(taskId)}`,
        changes,
      );
      this._upsert(updated);
      return updated;
    },

    async pauseTask(taskId: string): Promise<void> {
      const t = await apiJson<ScheduledTaskItem>(
        "POST",
        `${BASE}/${encodeURIComponent(taskId)}/pause`,
      );
      this._upsert(t);
    },

    async resumeTask(taskId: string): Promise<void> {
      const t = await apiJson<ScheduledTaskItem>(
        "POST",
        `${BASE}/${encodeURIComponent(taskId)}/resume`,
      );
      this._upsert(t);
    },

    async runTask(taskId: string): Promise<void> {
      await apiJson<{ queued: string }>(
        "POST",
        `${BASE}/${encodeURIComponent(taskId)}/run`,
      );
    },

    async removeTask(taskId: string): Promise<void> {
      await apiJson<{ removed: string }>(
        "DELETE",
        `${BASE}/${encodeURIComponent(taskId)}`,
      );
      this.tasks = this.tasks.filter((t) => t.task_id !== taskId);
    },

    /** Fetch one task's run history (newest first). Throws on failure. */
    async loadRuns(taskId: string, limit = 20): Promise<ScheduledTaskRun[]> {
      const res = await apiJson<RunsResponse>(
        "GET",
        `${BASE}/${encodeURIComponent(taskId)}/runs`,
        undefined,
        { query: { limit } },
      );
      return res.runs;
    },
  },
});
