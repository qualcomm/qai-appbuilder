// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Notifications store — persistent unread notification center for
 * scheduled-task results.
 *
 * ROOT-CAUSE FIX (migration 075 + the three ``/notifications/*`` REST
 * endpoints): the bell is a PROJECTION of the ``scheduling_task_run``
 * table, not a snapshot of live WebSocket traffic. Every WS reconnect
 * refetches the server's unread set and merges it in by ``id`` — a fire
 * that landed while the WS was silently dead surfaces the next time the
 * client comes back. WS ``scheduling.task_fired`` events remain the fast
 * live path but are no longer the single point of failure.
 *
 * Distinct from the toast store: a toast is a one-shot, auto-dismissing
 * hint ("something just arrived"); a notification here is a durable
 * unread entry that stays in the bell list until the user views it
 * (jumping to the conversation, or opening the global task's full
 * result) or dismisses it. Dismissal writes through to the server so a
 * subsequent reconnect does not resurrect the same entry.
 */
import { defineStore } from "pinia";

import { apiJson } from "@/api/http";

/** A single unread notification pointing at a finished scheduled task. */
export interface NotificationItem {
  /**
   * Stable de-dup key. In production this MUST be the server-side
   * ``run_id`` (``scheduling_task_run.id``) so a live WS event and a
   * subsequent reconnect-backfill of the SAME fire coalesce into one bell
   * entry, not one per transport path.
   */
  id: string;
  taskId: string;
  taskName: string;
  /** Whether the scheduled run succeeded. */
  ok: boolean;
  /** Target conversation; empty for a global (unscoped) task. */
  conversationId: string;
  /**
   * True when the task is global — no bound conversation, so 'view'
   * opens the full result inline (from run history) instead of jumping
   * to a chat.
   */
  isGlobal: boolean;
  /** First ~200 chars of the result text, for the list preview. */
  resultPreview: string;
  /** Arrival time (ms epoch), for ordering / display. */
  ts: number;
}

interface NotificationsState {
  items: NotificationItem[];
}

/** Server payload of ``GET /api/scheduled-tasks/notifications/unread``. */
interface UnreadNotificationDto {
  id: string;
  task_id: string;
  task_name: string;
  conversation_id: string;
  ok: boolean;
  result_preview: string;
  ran_at: string;
}

interface UnreadListResponseDto {
  notifications: UnreadNotificationDto[];
  total: number;
}

interface MarkResponseDto {
  marked: number;
}

export const useNotificationsStore = defineStore("notifications", {
  state: (): NotificationsState => ({
    items: [],
  }),
  getters: {
    /** Unread count — every item in the list is unread by definition. */
    unreadCount: (state): number => state.items.length,
  },
  actions: {
    /**
     * Add one unread notification. Ignores a blank id or a duplicate
     * (same id already queued) — de-dup mirrors usePermissionDialog.enqueue.
     * Newest first so the bell list reads top-down by recency.
     */
    enqueue(item: NotificationItem): void {
      if (typeof item.id !== "string" || item.id === "") return;
      if (this.items.some((n) => n.id === item.id)) return;
      this.items = [item, ...this.items];
    },

    /**
     * Pull EVERY unread notification the server has and merge into the
     * local store. Called on WS (re)connect — this is the DURABLE
     * transport that fixes the "fired while WS was silently dead" class
     * of bugs. De-dup is by ``id``; a run already in the store is
     * skipped, so a live client that stayed connected sees no duplicates
     * when the same fire ALSO backfills. Best-effort: a network failure
     * leaves the store untouched.
     */
    async fetchUnread(): Promise<void> {
      try {
        const dto = await apiJson<UnreadListResponseDto>(
          "GET",
          "/api/scheduled-tasks/notifications/unread",
        );
        if (!dto || !Array.isArray(dto.notifications)) return;
        // Server returns newest-first; iterate oldest-first so each
        // enqueue (which prepends) leaves the freshest run at index 0.
        for (let i = dto.notifications.length - 1; i >= 0; i--) {
          const n = dto.notifications[i];
          if (n === undefined) continue;
          if (typeof n.id !== "string" || n.id === "") continue;
          const conv =
            typeof n.conversation_id === "string" ? n.conversation_id : "";
          this.enqueue({
            id: n.id,
            taskId: n.task_id ?? "",
            taskName: n.task_name ?? n.task_id ?? "",
            ok: n.ok === true,
            conversationId: conv,
            isGlobal: conv === "",
            resultPreview:
              typeof n.result_preview === "string" ? n.result_preview : "",
            ts: Number.isNaN(Date.parse(n.ran_at))
              ? Date.now()
              : Date.parse(n.ran_at),
          });
        }
      } catch {
        // Silent — the next reconnect retries the backfill.
      }
    },

    /**
     * Dismiss ONE notification: remove locally AND tell the server so a
     * subsequent reconnect does not resurrect it. Local removal happens
     * even if the network write fails (the user's intent to dismiss must
     * not be blocked by a transient error).
     */
    async markRead(id: string): Promise<void> {
      const idx = this.items.findIndex((n) => n.id === id);
      if (idx >= 0) this.items.splice(idx, 1);
      try {
        await apiJson<MarkResponseDto>(
          "POST",
          `/api/scheduled-tasks/notifications/${encodeURIComponent(id)}/mark-read`,
        );
      } catch {
        // Silent; local optimistic removal already applied.
      }
    },

    /**
     * Bulk-dismiss every currently-visible notification. Same optimistic
     * shape as markRead: clear locally first, then attempt to persist.
     */
    async markAllRead(): Promise<void> {
      this.items = [];
      try {
        await apiJson<MarkResponseDto>(
          "POST",
          "/api/scheduled-tasks/notifications/mark-all-read",
        );
      } catch {
        // Silent; local clear already applied.
      }
    },

    /** Full local reset — used by tests + logout paths. Does not persist. */
    clear(): void {
      this.items = [];
    },
  },
});
