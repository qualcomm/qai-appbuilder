// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * usePermissionDialog — global file-access / command-execution authorization
 * dialog state (V1 parity: `js/security/use_security_dialog.js`).
 *
 * The backend PolicyCenter pushes a `permission_request` frame on the shared
 * `/api/events` SSE stream when a普通聊天 tool call hits an ASK decision
 * (`src/qai/security/domain/events.py:PermissionRequestedEvent.to_dict`):
 *
 *   { type: "permission_request", id, op, path, caller, channel,
 *     session_id, timestamp }
 *
 * The front-end responds via the locked permission routes:
 *
 *   - approve: `POST /api/security/permission/{id}/approve` body `{ grant }`
 *              where grant ∈ once | session | process | permanent
 *   - reject:  `POST /api/security/permission/{id}/reject`
 *   - ignore:  no API call — backend times out → DENY (V1 parity)
 *   - permanent: the grant itself goes through `/permission/{id}/approve` as
 *                above; the backend then persists a non-expiring grant and
 *                syncs it into the native guard allow-list (PR-4/PR-5), so
 *                cross-session persistence is now live (`persistPath()` is a
 *                post-approve confirmation hook).
 *
 * Design (mirrors V1):
 *   1. No new EventSource — App.vue's single `/api/events` connection forwards
 *      `permission_request` events into {@link enqueue}; SSE (re)connect calls
 *      {@link fetchPending} to pull未决项 the backend still holds.
 *   2. Multiple requests merge into a queue; only the head is shown + a
 *      `1/N` badge.
 *   3. `respondedIds` Set dedupes against SSE re-delivery / fetchPending
 *      echoing an already-handled request back.
 *   4. Optimistic dequeue with rollback + toast on a non-404 failure (404 =
 *      backend already resolved → stay dequeued).
 *
 * This is a module-level singleton (shared reactive state) so the dialog and
 * the App.vue SSE handler observe the same queue regardless of where the
 * composable is invoked.
 */
import { computed, ref, type ComputedRef, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson } from "@/api";
import { useToast } from "@/composables/useToast";

/** A single permission request as delivered on the `/api/events` SSE frame.
 *
 * Phase 2 (2026-07-06) additions:
 *   - `pid` / `process_path` / `command_line` — populated by the native-hook
 *     bridge so the UI can group requests per subprocess and let the user do
 *     a "cancel all from this process" batch action (plan §2.4).
 *   - `boot_id` — the backend PolicyCenter's boot identifier at the time the
 *     request was enqueued. If a later `boot_id` is seen (SSE reconnect after
 *     service restart, or a fresh `/pending` fetch) any older-boot requests
 *     are flagged with `is_orphan = true` so the dialog shows the
 *     "pre-restart" badge (plan §P3).
 *   - `is_orphan` — either the backend marked it (via a startup-fetch
 *     endpoint that lists surviving pending) or the frontend inferred it
 *     from the boot_id delta. Purely presentational.
 */
export interface PermissionRequestEvent {
  type?: string;
  id: string;
  op?: string;
  path?: string;
  caller?: string;
  channel?: string;
  session_id?: string;
  timestamp?: string;
  pid?: number;
  process_path?: string;
  command_line?: string;
  boot_id?: string;
  is_orphan?: boolean;
  /**
   * P-11 (2026-07-06): true when this request originates from a native
   * subprocess file event (guard64.dll hook) rather than an in-process tool
   * call. The backend always sets `scope_conversation_id=""` for native
   * events, so a "session"-scope grant can never match — the dialog therefore
   * disables (grays out) the session button for these requests. Absent /
   * undefined ⇒ treat as in-process (session enabled); never crash.
   */
  is_native_subprocess?: boolean;
  /**
   * P-EXEC (2026-07-06): optional human-readable rationale for *why* this
   * request needs manual confirmation — set by the exec-broker dangerous-
   * command ASK path (e.g. "git push --force rewrites remote history").
   * TAIL-appended, backward-compatible: absent for the plain FileGuard path
   * ASK, in which case the dialog renders no reason banner.
   */
  reason?: string;
}

/** Grant scope vocabulary (V1 `resolve_permission`). */
export type GrantScope = "deny" | "once" | "session" | "process" | "permanent";

/**
 * P-11B (2026-07-07): grant *range* — an orthogonal dimension to
 * {@link GrantScope}. Scope answers "how long is the grant remembered"
 * (once / session / process / permanent); range answers "how wide is it"
 * (this single file vs. the whole parent directory). Only meaningful for
 * file-path operations; the backend ignores it for exec commands and also
 * auto-falls-back to file-level when the derived parent directory is too
 * shallow (depth < 2), so the frontend never has to guard that.
 * Sent on the approve body as `grant_range` (defaults to "file" — the
 * pre-existing behaviour — so deny/reject and legacy callers are unaffected).
 */
export type GrantRange = "file" | "directory" | "program";

/** Pending-list response shape (`GET /api/security/permission/pending`). */
interface PendingEventsResponse {
  // V1 used `{ pending: [...] }`; V2's SSE-shaped pending list may surface the
  // same flat frames. Accept either key defensively so a backend wording
  // difference never silently drops the未决项.
  pending?: PermissionRequestEvent[];
  requests?: PermissionRequestEvent[];
}

// ── Module-level singleton state ──────────────────────────────────────────────
const pendingRequests = ref<PermissionRequestEvent[]>([]);
const respondedIds = new Set<string>();
const minimized = ref(false);
/**
 * The most recent `boot_id` observed on any incoming permission_request /
 * fetchPending payload. Any request whose `boot_id` is non-empty and does NOT
 * match this value is treated as an "orphan" (pre-restart) request per
 * plan §P3. First-ever request seeds this value; the dialog does not flag
 * anything as orphan until AT LEAST one non-empty boot_id has been seen
 * (otherwise, a backend that omits boot_id would look like every request is
 * an orphan of every other request).
 */
const currentBootId = ref<string>("");

function markOrphanFlag(req: PermissionRequestEvent): PermissionRequestEvent {
  // Backend-forced orphan wins.
  if (req.is_orphan === true) return req;
  const rb = typeof req.boot_id === "string" ? req.boot_id : "";
  const cb = currentBootId.value;
  if (rb !== "" && cb !== "" && rb !== cb) {
    // Same object but with the orphan flag lifted so downstream consumers
    // don't have to re-derive it. Non-destructive to the caller's copy.
    return { ...req, is_orphan: true };
  }
  return req;
}

function updateBootId(req: PermissionRequestEvent): void {
  const rb = typeof req.boot_id === "string" ? req.boot_id : "";
  if (rb === "") return;
  if (currentBootId.value === "") {
    // First non-empty boot_id ever seen — seed. Nothing is orphan yet.
    currentBootId.value = rb;
    return;
  }
  // If we see a newer boot_id (any non-matching non-empty value), adopt it as
  // the current one AND re-flag any older-boot queued items as orphans so the
  // dialog shows the pre-restart badge without waiting for a fresh fetch.
  if (rb !== currentBootId.value) {
    currentBootId.value = rb;
    for (const q of pendingRequests.value) {
      const qb = typeof q.boot_id === "string" ? q.boot_id : "";
      if (qb !== "" && qb !== rb) {
        q.is_orphan = true;
      }
    }
  }
}

const isVisible: ComputedRef<boolean> = computed(
  () => !minimized.value && pendingRequests.value.length > 0,
);
const currentRequest: ComputedRef<PermissionRequestEvent | null> = computed(
  () => pendingRequests.value[0] ?? null,
);
const queueCount: ComputedRef<number> = computed(
  () => pendingRequests.value.length,
);

function enqueue(req: PermissionRequestEvent | null | undefined): void {
  if (!req || typeof req.id !== "string" || req.id === "") return;
  if (respondedIds.has(req.id)) return;
  if (pendingRequests.value.some((r) => r.id === req.id)) return;
  // Phase 2: seed / update the boot_id "current" tracker, then mark orphan.
  updateBootId(req);
  pendingRequests.value.push(markOrphanFlag(req));
  // A new request arriving re-expands a minimized dialog (V1 parity).
  if (minimized.value) minimized.value = false;
}

function dequeue(id: string): void {
  const idx = pendingRequests.value.findIndex((r) => r.id === id);
  if (idx >= 0) pendingRequests.value.splice(idx, 1);
  respondedIds.add(id);
}

/**
 * Pull未决 permission requests the backend still holds (called on SSE
 * reconnect so requests pushed while disconnected are not lost). Degrades
 * silently — the endpoint may be transiently unavailable.
 */
async function fetchPending(): Promise<void> {
  try {
    const data = await apiJson<PendingEventsResponse>(
      "GET",
      "/api/security/permission/pending",
    );
    const list = Array.isArray(data.pending)
      ? data.pending
      : Array.isArray(data.requests)
        ? data.requests
        : [];
    for (const req of list) enqueue(req);
  } catch {
    // silent — do not disturb the user on a transient pending-fetch hiccup
  }
}

export interface UsePermissionDialog {
  pendingRequests: Ref<PermissionRequestEvent[]>;
  minimized: Ref<boolean>;
  isVisible: ComputedRef<boolean>;
  currentRequest: ComputedRef<PermissionRequestEvent | null>;
  queueCount: ComputedRef<number>;
  currentBootId: Ref<string>;
  enqueue: (req: PermissionRequestEvent | null | undefined) => void;
  fetchPending: () => Promise<void>;
  respond: (id: string, grant: GrantScope, range?: GrantRange) => Promise<void>;
  respondAll: (grant: GrantScope) => Promise<void>;
  ignoreCurrent: () => void;
  persistPath: (path: string) => Promise<void>;
  minimize: () => void;
  restore: () => void;
  // Phase 2 additions (plan §P5 / §2.4).
  cancel: (id: string) => Promise<void>;
  cancelAllForPid: (pid: number) => Promise<void>;
  cancelAll: () => Promise<void>;
}

export function usePermissionDialog(): UsePermissionDialog {
  const toast = useToast();
  const { t } = useI18n();

  /**
   * Respond to one request with a grant scope. Optimistically dequeues; on a
   * non-404 failure the request is restored to the head of the queue and a
   * toast is shown (V1 parity).
   *
   * P-11B: `range` ("file" default / "directory") is the orthogonal grant
   * width dimension — only forwarded on the approve body as `grant_range`.
   * Defaults to "file" so deny/reject, the Enter shortcut and the bulk
   * "allow all" callers keep their pre-existing per-file behaviour untouched.
   */
  async function respond(
    id: string,
    grant: GrantScope,
    range: GrantRange = "file",
  ): Promise<void> {
    const req = pendingRequests.value.find((r) => r.id === id);
    if (!req) return;
    // P-11 — a "session" grant is a dead option for native-subprocess file
    // events (the backend attributes them with an empty conversation id, so a
    // session-scoped grant can never match at the native layer). The single
    // session button is grayed out for those, but the bulk "allow all" button
    // hard-codes "session"; downgrade it to "process" here (the narrowest
    // scope that IS valid for native — it matches this process's boot id) so
    // an "allow all" over a queue containing native events does not silently
    // create grants that never match and immediately re-prompt.
    if (grant === "session" && req.is_native_subprocess === true) {
      grant = "process";
    }
    // Optimistic dequeue — prevents double-click re-submits.
    dequeue(id);
    try {
      if (grant === "deny") {
        await apiJson("POST", `/api/security/permission/${id}/reject`, {
          reason: "",
        });
      } else {
        await apiJson("POST", `/api/security/permission/${id}/approve`, {
          grant,
          grant_range: range,
        });
        // A permanent grant also confirms cross-session persistence. The
        // approve route above already persisted a non-expiring grant AND
        // synced it into the native guard allow-list (PR-4); `persistPath`
        // is the post-approve confirmation hook (no duplicate write).
        if (grant === "permanent" && typeof req.path === "string" && req.path) {
          await persistPath(req.path);
        }
      }
    } catch (e) {
      const status = (e as { status?: number } | null)?.status;
      if (status === 404) {
        // Backend already resolved (timeout / duplicate) — stay dequeued.
        return;
      }
      // Restore to the head + surface a toast so the user can retry.
      pendingRequests.value.unshift(req);
      respondedIds.delete(id);
      const msg = `${t("security.respondFailed")}${
        e instanceof Error ? e.message : String(e)
      }`;
      toast.error(msg);
    }
  }

  /** Respond to every pending request with the same grant (serially). */
  async function respondAll(grant: GrantScope): Promise<void> {
    const snapshot = pendingRequests.value.slice();
    for (const req of snapshot) {
      // eslint-disable-next-line no-await-in-loop -- serial: avoid hammering backend
      await respond(req.id, grant);
    }
  }

  /** Ignore the current request: no API call, rely on the backend timeout. */
  function ignoreCurrent(): void {
    const req = currentRequest.value;
    if (!req) return;
    dequeue(req.id);
  }

  /**
   * Confirm cross-session persistence of a `permanent`-scope grant.
   *
   * Restored 2026-07-04 (native FileGuard integration, PR-5). Cross-session
   * persistence is now wired end-to-end through the locked approve route:
   * `POST /api/security/permission/{id}/approve` with `{ grant: "permanent" }`
   * makes the backend persist a NON-EXPIRING `PathGrant`
   * (`ApprovePermissionUseCase._persist_grant`, PR-4) AND synchronise it into
   * the native guard64.dll allow-list (`PathGrantCreatedEvent` →
   * `NativeFileGuard.add_allow_rule`, PR-4). So by the time this runs the path
   * is ALREADY durably granted — issuing a second `create_grant` here would
   * conflict (one grant per subject+path). This hook is therefore a light
   * post-approve confirmation, kept on the public API so callers / tests keep
   * a stable surface and a future per-path persistence UI can extend it.
   *
   * The old `POST /api/security/persistent_acl/user_paths` route (Windows ACL
   * backend, removed 2026-07-01) is intentionally NOT called.
   */
  async function persistPath(path: string): Promise<void> {
    if (!path) return;
    // Persistence already happened via the approve route (see above). Nothing
    // more to send; a duplicate grant write would fail with a conflict.
  }

  function minimize(): void {
    minimized.value = true;
  }
  function restore(): void {
    minimized.value = false;
  }

  /**
   * Phase 2 (plan §P5 / §2.6): user-initiated cancel — distinct from `deny`
   * in audit semantics (`user_cancelled` vs `user_denied`). Native hook still
   * returns `False` for the underlying operation. Backend endpoint:
   *
   *   POST /api/security/permission/cancel
   *   body: { request_id: <id> } | { pid: <int> } | { cancel_all: true }
   *
   * All three variants are optimistic-dequeue: the local queue is trimmed
   * immediately; a non-404 failure re-enqueues at the head and shows a toast.
   * 404 = backend already resolved (subprocess_gone / concurrent user
   * action) → stay dequeued.
   */
  async function cancel(id: string): Promise<void> {
    const req = pendingRequests.value.find((r) => r.id === id);
    if (!req) return;
    dequeue(id);
    try {
      await apiJson("POST", "/api/security/permission/cancel", {
        request_id: id,
      });
    } catch (e) {
      const status = (e as { status?: number } | null)?.status;
      if (status === 404) return; // already resolved, stay dequeued
      pendingRequests.value.unshift(req);
      respondedIds.delete(id);
      const msg = `${t("security.respondFailed")}${
        e instanceof Error ? e.message : String(e)
      }`;
      toast.error(msg);
    }
  }

  /**
   * Cancel every pending request originating from a specific pid (plan §2.4:
   * "cancel all from this process" affordance). Optimistically dequeues the
   * matching subset; a failure re-enqueues the batch and shows a toast.
   */
  async function cancelAllForPid(pid: number): Promise<void> {
    if (!Number.isFinite(pid) || pid <= 0) return;
    const snapshot = pendingRequests.value.filter((r) => r.pid === pid);
    if (snapshot.length === 0) return;
    // Optimistic batch dequeue.
    for (const r of snapshot) dequeue(r.id);
    try {
      await apiJson("POST", "/api/security/permission/cancel", { pid });
    } catch (e) {
      const status = (e as { status?: number } | null)?.status;
      if (status === 404) return;
      // Restore in original order at the head.
      for (let i = snapshot.length - 1; i >= 0; i--) {
        const item = snapshot[i];
        if (!item) continue;
        pendingRequests.value.unshift(item);
        respondedIds.delete(item.id);
      }
      const msg = `${t("security.respondFailed")}${
        e instanceof Error ? e.message : String(e)
      }`;
      toast.error(msg);
    }
  }

  /**
   * Cancel every pending request across all processes (emergency exit —
   * plan §P5 `{"cancel_all": true}`). Same optimistic-with-rollback pattern.
   */
  async function cancelAll(): Promise<void> {
    const snapshot = pendingRequests.value.slice();
    if (snapshot.length === 0) return;
    for (const r of snapshot) dequeue(r.id);
    try {
      await apiJson("POST", "/api/security/permission/cancel", {
        cancel_all: true,
      });
    } catch (e) {
      const status = (e as { status?: number } | null)?.status;
      if (status === 404) return;
      for (let i = snapshot.length - 1; i >= 0; i--) {
        const item = snapshot[i];
        if (!item) continue;
        pendingRequests.value.unshift(item);
        respondedIds.delete(item.id);
      }
      const msg = `${t("security.respondFailed")}${
        e instanceof Error ? e.message : String(e)
      }`;
      toast.error(msg);
    }
  }

  return {
    pendingRequests,
    minimized,
    isVisible,
    currentRequest,
    queueCount,
    currentBootId,
    enqueue,
    fetchPending,
    respond,
    respondAll,
    ignoreCurrent,
    persistPath,
    minimize,
    restore,
    cancel,
    cancelAllForPid,
    cancelAll,
  };
}
