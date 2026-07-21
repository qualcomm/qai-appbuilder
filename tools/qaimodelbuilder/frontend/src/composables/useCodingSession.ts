// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useCodingSession(kind)` — shared core for the Claude Code (`cc`) and
 * Open Code (`oc`) chat-side working surfaces (功能块 6 + 7).
 *
 * CC and OC are interface-symmetric, so both providers share one
 * module-level singleton state machine keyed by `kind` (see
 * `coding/sessionContext.ts`). The thin `useClaudeCode` / `useOpenCode`
 * wrappers just bind a `kind` and re-export the surface.
 *
 * **Cohesion split (2026-06-09)**: this was a single ~1240-line "god
 * composable" mixing five independent responsibilities. It is now a thin
 * ORCHESTRATION layer that builds the shared `CodingSessionContext` and
 * assembles four focused slice factories, mirroring the existing
 * `coding/frameProcessing.ts` pattern:
 *
 *   - `useSessionCrud`     — session list / start-stop-delete-rename /
 *                            working-dir / active selection / mode-panel
 *   - `useSessionContext`  — context+effort+interrupt / permission / progress
 *   - `useSessionStream`   — two-step send + SSE + queue + locks
 *   - `useSessionHistory`  — history list / restore-fork / rewind / IM notify
 *
 * Cross-slice dependencies are wired here by injection (stream ← context's
 * `refreshContextUsage`; history ← crud's `fetchSessions`) so no slice
 * reaches into another — the public surface is byte-for-byte identical to
 * the pre-split implementation (zero behaviour change).
 *
 * Design decisions (V2-authoritative, see
 * docs/90-refactor/archive/feature-specs/v2-feature-spec-block67.md) —
 * SSE frame contract, per-session message isolation, two-step send, V2
 * schema field names — are documented at the slice/`frameProcessing` level.
 */
import { makeCodingSessionContext } from "./coding/sessionContext";
import { useSessionCrud } from "./coding/useSessionCrud";
import { useSessionContext } from "./coding/useSessionContext";
import { useSessionStream } from "./coding/useSessionStream";
import { useSessionHistory } from "./coding/useSessionHistory";
import type { CodingKind } from "./useCodingSession.types";

// Re-export the public types so existing consumers keep importing them
// from `@/composables/useCodingSession` unchanged.
export type {
  CodingKind,
  CodingSession,
  ToolCallStatus,
  CodingToolCall,
  CodingSubTaskUsage,
  CodingSubTask,
  CodingMessage,
  PermissionRequest,
  QueuedMessage,
  SessionProgress,
  HistorySession,
} from "./useCodingSession.types";

// ─── Composable orchestration layer ──────────────────────────────────────────

export function useCodingSession(kind: CodingKind) {
  const ctx = makeCodingSessionContext(kind);
  const { st } = ctx;

  const crud = useSessionCrud(ctx);
  const context = useSessionContext(ctx);
  const stream = useSessionStream(ctx, {
    refreshContextUsage: context.refreshContextUsage,
  });
  const history = useSessionHistory(ctx, {
    fetchSessions: crud.fetchSessions,
  });

  return {
    kind,
    // state
    isMode: st.isMode,
    panelOpen: st.panelOpen,
    sessions: st.sessions,
    activeSessionId: st.activeSessionId,
    activeSession: crud.activeSession,
    messages: crud.activeMessages,
    loading: st.loading,
    streaming: st.streaming,
    streamingSessionId: st.streamingSessionId,
    currentModel: st.currentModel,
    pendingPermission: st.pendingPermission,
    // queue + progress (V1 ccQueue / sessionProgress)
    queue: stream.queue,
    removeFromQueue: stream.removeFromQueue,
    activeProgress: context.activeProgress,
    progressFor: context.progressFor,
    // history list (V1 historySessions)
    historySessions: history.historySessions,
    historyLoading: history.historyLoading,
    // session CRUD
    fetchSessions: crud.fetchSessions,
    fetchCurrentModel: crud.fetchCurrentModel,
    startSession: crud.startSession,
    quickNewSession: crud.quickNewSession,
    stopSession: crud.stopSession,
    deleteSession: crud.deleteSession,
    deleteSessionPermanent: crud.deleteSessionPermanent,
    renameSession: crud.renameSession,
    changeWorkingDir: crud.changeWorkingDir,
    setActive: crud.setActive,
    enterMode: crud.enterMode,
    exitMode: crud.exitMode,
    togglePanel: crud.togglePanel,
    collapsePanel: crud.collapsePanel,
    // messaging
    sendMessage: stream.sendMessage,
    // permissions
    decidePermission: context.decidePermission,
    dismissPermission: context.dismissPermission,
    // context / effort / interrupt
    refreshContextUsage: context.refreshContextUsage,
    setEffort: context.setEffort,
    interrupt: context.interrupt,
    // history / fork / checkpoints / channel notify
    loadHistorySessions: history.loadHistorySessions,
    loadSessionHistory: history.loadSessionHistory,
    restoreSession: history.restoreSession,
    rewindFiles: history.rewindFiles,
    setWechatNotify: history.setWechatNotify,
    setFeishuNotify: history.setFeishuNotify,
  };
}

export type CodingSessionApi = ReturnType<typeof useCodingSession>;
