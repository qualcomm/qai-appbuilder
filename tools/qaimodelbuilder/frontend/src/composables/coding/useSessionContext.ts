// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Context / effort / interrupt / permission / progress slice of
 * `useCodingSession` (cohesion split). Owns the per-session token/context
 * refresh (V2 REST), effort setting, turn interrupt, pending-permission
 * decisions, and the progress-indicator accessors. Reads/writes the
 * shared `CodingSessionContext`.
 */
import { computed, type ComputedRef } from "vue";

import { apiJson, ApiError } from "@/api";
import type { CodingSessionContext } from "./sessionContext";
import type {
  CodingMessage,
  ContextSizeResponse,
  ContextUsageResponse,
  SessionProgress,
} from "../useCodingSession.types";

export interface SessionContextSlice {
  activeProgress: ComputedRef<SessionProgress | null>;
  progressFor: (sessionId: string) => SessionProgress | null;
  refreshContextUsage: (sessionId: string, assistantMsg?: CodingMessage) => Promise<void>;
  setEffort: (sessionId: string, effort: string | null) => Promise<void>;
  interrupt: (sessionId?: string) => Promise<void>;
  decidePermission: (
    requestId: string,
    sessionId: string,
    decision: "approved" | "rejected",
    updatedPermissions?: unknown[],
  ) => Promise<void>;
  dismissPermission: () => void;
}

export function useSessionContext(ctx: CodingSessionContext): SessionContextSlice {
  const { kind, st, prefix, t, toastError } = ctx;

  // ── Progress accessor (V1 sessionProgress / activeSessionProgress) ───────────
  const activeProgress = computed<SessionProgress | null>(() => {
    const id = st.activeSessionId.value;
    if (id === null) return null;
    return st.sessionProgress[id] ?? null;
  });

  function progressFor(sessionId: string): SessionProgress | null {
    return st.sessionProgress[sessionId] ?? null;
  }

  /**
   * Refresh per-session token / context counters via V2 REST.
   *
   * V1 read these off the `done` SSE payload (`useClaudeCode.js:913-943`);
   * V2's `event: done` payload is `{}` — counters are fetched via REST so
   * the wire stays decoupled from provider counter semantics. Two routes
   * are queried because the schemas differ (CC `context_usage` session-level
   * vs OC `context_size` per-turn). Counters carry the aggregate's REAL
   * cumulative usage (U-010 / 2-H2); a value of 0 is the initial state (no
   * round streamed yet), not a stub. `assistantMsg` (optional): mirrors the
   * per-turn deltas onto its `usage` field so the per-row token badge can
   * render.
   */
  async function refreshContextUsage(
    sessionId: string,
    assistantMsg?: CodingMessage,
  ): Promise<void> {
    // CC-flavoured payload (session-level percentage / max).
    try {
      const res = await apiJson<ContextUsageResponse>(
        "GET",
        `${prefix}/sessions/${sessionId}/context_usage`,
      );
      const s = st.sessions.value.find((x) => x.session_id === sessionId);
      if (s !== undefined && res.ok) {
        s.context_total_tokens = res.totalTokens;
        s.context_max_tokens = res.maxTokens;
        s.context_percentage = res.percentage;
        // Mirror to context_window so the V1-named field (consumed by
        // the session-list ctx badge) stays populated regardless of
        // which route surfaced the value.
        if (s.context_window === undefined || s.context_window === 0) {
          s.context_window = res.maxTokens;
        }
      }
    } catch {
      // Context badge is best-effort; ignore failures.
    }

    // OC-flavoured payload (per-turn deltas) — only OC sessions have
    // this route, but the call is harmless on CC (404 swallowed).
    if (kind === "oc") {
      try {
        const sz = await apiJson<ContextSizeResponse>(
          "GET",
          `${prefix}/sessions/${sessionId}/context_size`,
        );
        const s = st.sessions.value.find((x) => x.session_id === sessionId);
        if (s !== undefined) {
          s.last_input_tokens = sz.last_input_tokens;
          s.total_input_tokens = sz.total_input_tokens;
          s.context_window = sz.context_limit;
          s.turn_count = sz.turn_count;
          s.total_tool_calls = sz.total_tool_calls;
        }
        if (assistantMsg !== undefined) {
          assistantMsg.usage = {
            inputTokens: sz.last_input_tokens,
            outputTokens: sz.total_output_tokens,
            totalTokens: sz.last_input_tokens + sz.total_output_tokens,
          };
        }
      } catch {
        // Best-effort; CC sessions don't expose this route.
      }
    }
  }

  async function setEffort(sessionId: string, effort: string | null): Promise<void> {
    try {
      await apiJson("POST", `${prefix}/sessions/${sessionId}/effort`, { effort });
      const s = st.sessions.value.find((x) => x.session_id === sessionId);
      if (s !== undefined) s.effort = effort;
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : "Failed to set effort");
    }
  }

  async function interrupt(sessionId?: string): Promise<void> {
    const id = sessionId ?? st.activeSessionId.value;
    if (id === null) return;
    try {
      await apiJson("POST", `${prefix}/sessions/${id}/interrupt`);
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : t("claudeCode.stopTaskFailed", { msg: "" }));
    }
    // Abort the local stream regardless (best-effort).
    if (st.streamingSessionId.value === id) {
      st.abortController?.abort();
      st.abortController = null;
      st.sessionProgress[id] = null;
      st.sendingLock = false;
      st.streaming.value = false;
      st.streamingSessionId.value = null;
    }
  }

  /**
   * Decide a pending permission request via
   * `POST {prefix}/permissions/{request_id}/decide`. `decision ∈
   * {approved, rejected}` (V2 `PermissionDecision`). "Allow & Remember"
   * forwards `updated_permissions` (V1 parity AiCodingPanel.js:583-598);
   * the field is sent as-is so it lights up when the backend wires it
   * (待 SDK; standard approve still works without it).
   */
  async function decidePermission(
    requestId: string,
    sessionId: string,
    decision: "approved" | "rejected",
    updatedPermissions?: unknown[],
  ): Promise<void> {
    try {
      const body: Record<string, unknown> = { session_id: sessionId, decision };
      if (updatedPermissions !== undefined && updatedPermissions.length > 0) {
        body["updated_permissions"] = updatedPermissions;
      }
      await apiJson("POST", `${prefix}/permissions/${requestId}/decide`, body);
      if (st.pendingPermission.value?.request_id === requestId) {
        st.pendingPermission.value = null;
      }
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : "Failed to decide permission");
    }
  }

  function dismissPermission(): void {
    st.pendingPermission.value = null;
  }

  return {
    activeProgress,
    progressFor,
    refreshContextUsage,
    setEffort,
    interrupt,
    decidePermission,
    dismissPermission,
  };
}
