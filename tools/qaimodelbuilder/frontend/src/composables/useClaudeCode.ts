// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useClaudeCode` — Claude Code chat-side working surface (功能块 6).
 *
 * Thin wrapper over the shared {@link useCodingSession} core bound to the
 * `cc` provider (`/api/cc/*`). The full CC working interface (per-session
 * message isolation, tool-call streaming, real-time permission approval,
 * context badge, effort, interrupt, rename / working-dir, history) lives
 * in the shared core so Open Code (块 7) reuses the same machinery.
 *
 * Public surface is **backward compatible** with the previous T2.7-B
 * skeleton (`isCCMode` / `enterCCMode` / `exitCCMode` / `messages` /
 * `streaming` / `activeSession` / `activeSessionId` / `currentModel` /
 * `fetchSessions` / `fetchCurrentModel` / `startSession` / `stopSession` /
 * `setActive` / `sendMessage`) so existing consumers (ChatView,
 * ChatComposer pill, AiCodingPanel) keep compiling, plus the new rich
 * surface (tool calls, permissions, effort, …).
 *
 * SSE frame contract + payload shapes: see {@link useCodingSession}
 * (V2-authoritative, captured in docs/90-refactor/archive/feature-specs/v2-feature-spec-block67.md 阶段0).
 */
import { useCodingSession } from "./useCodingSession";
import type {
  CodingSession,
  CodingMessage,
  CodingToolCall,
  ToolCallStatus,
  PermissionRequest,
} from "./useCodingSession";

// Re-export shared types under the legacy CC names for downstream imports.
export type ClaudeCodeSession = CodingSession;
export type ClaudeCodeMessage = CodingMessage;
export type ClaudeCodeToolCall = CodingToolCall;
export type { ToolCallStatus, PermissionRequest };

export function useClaudeCode() {
  const core = useCodingSession("cc");

  return {
    // ── legacy-compatible surface ──────────────────────────────────────────
    isCCMode: core.isMode,
    sessions: core.sessions,
    activeSessionId: core.activeSessionId,
    activeSession: core.activeSession,
    messages: core.messages,
    loading: core.loading,
    streaming: core.streaming,
    currentModel: core.currentModel,
    fetchSessions: core.fetchSessions,
    fetchCurrentModel: core.fetchCurrentModel,
    startSession: core.startSession,
    quickNewSession: core.quickNewSession,
    stopSession: core.stopSession,
    deleteSession: core.deleteSession,
    setActive: core.setActive,
    enterCCMode: core.enterMode,
    exitCCMode: core.exitMode,
    sendMessage: core.sendMessage,
    // ── floating panel visibility (V1 ccPanelOpen) ───────────────────────────
    panelOpen: core.panelOpen,
    togglePanel: core.togglePanel,
    collapsePanel: core.collapsePanel,
    // ── new rich surface (功能块 6) ─────────────────────────────────────────
    streamingSessionId: core.streamingSessionId,
    pendingPermission: core.pendingPermission,
    deleteSessionPermanent: core.deleteSessionPermanent,
    renameSession: core.renameSession,
    changeWorkingDir: core.changeWorkingDir,
    decidePermission: core.decidePermission,
    dismissPermission: core.dismissPermission,
    refreshContextUsage: core.refreshContextUsage,
    setEffort: core.setEffort,
    interrupt: core.interrupt,
    // ── queue / progress (V1 ccQueue / sessionProgress) ──────────────────────
    queue: core.queue,
    removeFromQueue: core.removeFromQueue,
    activeProgress: core.activeProgress,
    progressFor: core.progressFor,
    // ── history / fork / checkpoints / channel notify (V1) ───────────────────
    historySessions: core.historySessions,
    historyLoading: core.historyLoading,
    loadHistorySessions: core.loadHistorySessions,
    loadSessionHistory: core.loadSessionHistory,
    restoreSession: core.restoreSession,
    rewindFiles: core.rewindFiles,
    setWechatNotify: core.setWechatNotify,
    setFeishuNotify: core.setFeishuNotify,
  };
}
