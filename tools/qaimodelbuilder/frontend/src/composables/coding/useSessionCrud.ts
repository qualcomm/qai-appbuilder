// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Session CRUD + mode/panel control slice of `useCodingSession`
 * (cohesion split). Owns session list fetch, start/stop/delete/rename,
 * working-dir change, active-session selection, and the mode/panel
 * enter/exit/toggle. Reads/writes the shared `CodingSessionContext`.
 */
import { computed, type ComputedRef, type Ref } from "vue";

import { apiJson, ApiError } from "@/api";
import type { CodingSessionContext } from "./sessionContext";
import type {
  CodingMessage,
  CodingSession,
  ConfigResponse,
  SessionListResponse,
} from "../useCodingSession.types";

export interface SessionCrudSlice {
  activeSession: ComputedRef<CodingSession | null>;
  activeMessages: ComputedRef<CodingMessage[]>;
  currentModel: Ref<string>;
  fetchSessions: () => Promise<void>;
  fetchCurrentModel: () => Promise<void>;
  startSession: (workspace: string, title?: string) => Promise<CodingSession | null>;
  stopSession: (sessionId?: string) => Promise<void>;
  deleteSessionPermanent: (sessionId: string) => Promise<void>;
  deleteSession: (sessionId: string) => Promise<void>;
  renameSession: (sessionId: string, title: string) => Promise<void>;
  changeWorkingDir: (sessionId: string, workspace: string) => Promise<void>;
  setActive: (sessionId: string) => void;
  quickNewSession: () => Promise<CodingSession | null>;
  enterMode: () => Promise<void>;
  exitMode: () => void;
  togglePanel: () => void;
  collapsePanel: () => void;
}

export function useSessionCrud(ctx: CodingSessionContext): SessionCrudSlice {
  const { st, prefix, t, ensureBucket, toastError } = ctx;
  const { isMode, panelOpen, streaming } = st;

  const activeSession = computed<CodingSession | null>(() => {
    if (st.activeSessionId.value === null) return null;
    return st.sessions.value.find((s) => s.session_id === st.activeSessionId.value) ?? null;
  });

  /** Messages for the active session (per-session isolation). */
  const activeMessages = computed<CodingMessage[]>(() => {
    const id = st.activeSessionId.value;
    if (id === null) return [];
    return st.sessionMessages[id] ?? [];
  });

  async function fetchSessions(): Promise<void> {
    st.loading.value = true;
    try {
      const res = await apiJson<SessionListResponse>("GET", `${prefix}/sessions`);
      st.sessions.value = res.sessions;
      st.sessionsLoaded = true;
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : "Failed to fetch sessions");
    } finally {
      st.loading.value = false;
    }
  }

  async function fetchCurrentModel(): Promise<void> {
    try {
      const res = await apiJson<ConfigResponse>("GET", `${prefix}/config`);
      const m = res.config?.model;
      st.currentModel.value = typeof m === "string" ? m : "";
    } catch {
      st.currentModel.value = "";
    }
  }

  async function startSession(workspace: string, title?: string): Promise<CodingSession | null> {
    st.loading.value = true;
    try {
      const res = await apiJson<CodingSession>("POST", `${prefix}/sessions`, {
        workspace,
        title: title ?? null,
      });
      st.sessions.value = [...st.sessions.value, res];
      st.activeSessionId.value = res.session_id;
      ensureBucket(res.session_id);
      return res;
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : "Failed to start session");
      return null;
    } finally {
      st.loading.value = false;
    }
  }

  async function stopSession(sessionId?: string): Promise<void> {
    const id = sessionId ?? st.activeSessionId.value;
    if (id === null) return;
    if (st.streamingSessionId.value === id) {
      st.abortController?.abort();
      st.abortController = null;
      streaming.value = false;
      st.streamingSessionId.value = null;
    }
    try {
      await apiJson("DELETE", `${prefix}/sessions/${id}`);
      st.sessions.value = st.sessions.value.filter((s) => s.session_id !== id);
      delete st.sessionMessages[id];
      if (st.activeSessionId.value === id) {
        st.activeSessionId.value = st.sessions.value[0]?.session_id ?? null;
      }
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : "Failed to stop session");
    }
  }

  async function deleteSessionPermanent(sessionId: string): Promise<void> {
    try {
      await apiJson("DELETE", `${prefix}/sessions/${sessionId}/permanent`);
      st.sessions.value = st.sessions.value.filter((s) => s.session_id !== sessionId);
      delete st.sessionMessages[sessionId];
      if (st.activeSessionId.value === sessionId) {
        st.activeSessionId.value = st.sessions.value[0]?.session_id ?? null;
      }
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : "Failed to delete session");
    }
  }

  async function renameSession(sessionId: string, title: string): Promise<void> {
    try {
      // Wire field is `name` (RenameRequest); V1 parity (AiCodingPanel.js
      // rename posts `{ name }`). Sending `title` returns HTTP 400.
      await apiJson("POST", `${prefix}/sessions/${sessionId}/rename`, { name: title });
      const s = st.sessions.value.find((x) => x.session_id === sessionId);
      if (s !== undefined) s.title = title;
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : "Rename failed");
    }
  }

  async function changeWorkingDir(sessionId: string, workspace: string): Promise<void> {
    try {
      await apiJson("POST", `${prefix}/sessions/${sessionId}/working_dir`, {
        working_dir: workspace,
      });
      const s = st.sessions.value.find((x) => x.session_id === sessionId);
      if (s !== undefined) s.workspace = workspace;
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : "Failed to change working directory");
    }
  }

  function setActive(sessionId: string): void {
    if (st.sessions.value.some((s) => s.session_id === sessionId)) {
      st.activeSessionId.value = sessionId;
      ensureBucket(sessionId);
    }
  }

  /**
   * Permanently delete a session record (V1 useClaudeCode.js:1572 deleteSession).
   * Distinct from `stopSession` (close active) — this removes the persisted
   * record. Thin alias kept for V1 naming parity in the panel UI.
   */
  async function deleteSession(sessionId: string): Promise<void> {
    await deleteSessionPermanent(sessionId);
  }

  /**
   * Quick-new same-directory session (V1 useClaudeCode.js:1896 quickNewSession).
   * Inherits the active session's workspace; auto-increments a `#N` suffix on
   * the session name (`name #2`, `name #3`, …). Active session must exist.
   */
  async function quickNewSession(): Promise<CodingSession | null> {
    const current = st.activeSessionId.value;
    if (current === null) {
      toastError(t("claudeCode.noActiveSessionForQuickNew"));
      return null;
    }
    const session = st.sessions.value.find((s) => s.session_id === current);
    if (session === undefined) {
      toastError(t("claudeCode.noActiveSessionForQuickNew"));
      return null;
    }
    const workspace = session.workspace;
    const currentName = session.title ?? "";
    let newName: string;
    const m = currentName.match(/^(.*?)\s*#(\d+)$/);
    if (m !== null) {
      const base = m[1] ?? "";
      const n = parseInt(m[2] ?? "1", 10);
      newName = `${base} #${n + 1}`;
    } else {
      newName = currentName === "" ? "" : `${currentName} #2`;
    }
    return await startSession(workspace, newName === "" ? undefined : newName);
  }

  async function enterMode(): Promise<void> {
    isMode.value = true;
    panelOpen.value = true;
    if (!st.sessionsLoaded) {
      await fetchSessions();
    }
    if (st.activeSessionId.value === null && st.sessions.value.length > 0) {
      st.activeSessionId.value = st.sessions.value[0]!.session_id;
    }
    void fetchCurrentModel();
  }

  function exitMode(): void {
    st.abortController?.abort();
    st.abortController = null;
    streaming.value = false;
    st.streamingSessionId.value = null;
    isMode.value = false;
    panelOpen.value = false;
  }

  /**
   * Toggle the floating session panel (V1 `onClickCCPill`, app.js:728-737).
   * If the mode is off, entering it opens the panel; if already on, this
   * flips panel visibility without leaving the mode.
   */
  function togglePanel(): void {
    if (isMode.value) {
      panelOpen.value = !panelOpen.value;
    } else {
      void enterMode();
    }
  }

  /** Collapse (hide) the panel only — keeps the mode (V1 handleCollapsePanel). */
  function collapsePanel(): void {
    panelOpen.value = false;
  }

  return {
    activeSession,
    activeMessages,
    currentModel: st.currentModel,
    fetchSessions,
    fetchCurrentModel,
    startSession,
    stopSession,
    deleteSessionPermanent,
    deleteSession,
    renameSession,
    changeWorkingDir,
    setActive,
    quickNewSession,
    enterMode,
    exitMode,
    togglePanel,
    collapsePanel,
  };
}
