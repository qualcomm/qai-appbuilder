// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useOpenCode` — Open Code (OC) chat-side working surface (功能块 7).
 *
 * Thin wrapper over the shared {@link useCodingSession} core bound to the
 * `oc` provider (`/api/oc/*`). OC is interface-symmetric with Claude Code,
 * so it reuses the same machinery: per-session message isolation,
 * two-step send-then-stream, V2 `{kind,sequence,payload}` SSE parsing,
 * tool-call streaming (call_id pairing handled by the shared core), and
 * context / effort / interrupt.
 *
 * OC-specific surface added on top of the shared core (spec block7 §差异点):
 *   - `enterMode` / `exitMode` — re-exported from the core (CC exposed them
 *     under `enterCCMode` / `exitCCMode`; OC pill / ChatView need raw names).
 *   - `fetchHealth` — `GET /api/oc/health` → `{providers[], models[]}`,
 *     the candidate provider/model list source (PR-105 folded the old
 *     `/api/oc/providers` + `/api/oc/models` routes into health).
 *   - `selectModel` — writes the chosen model (and optionally provider) via
 *     `PUT /api/oc/config {model_id, provider_id?}` (零后端改动路径, spec
 *     §"选模型 provider_id/model_id" 推荐方式). Refreshes `currentModel`.
 *   - `revert` — `POST /api/oc/sessions/{id}/revert {message_id, part_id?}`
 *     (后端 `RevertRequest` 已存在; 核心未暴露, OC 在此薄包装).
 *
 * OC does NOT implement approval: the OC provider never emits a
 * `permission_request` frame, so `pendingPermission` stays `null` and the
 * permission dialog auto-hides — no extra code needed (spec §差异点).
 *
 * Public surface is backward compatible with the previous skeleton
 * (`isOCMode` / `sessions` / `activeSession` / `messages` / `streaming` /
 * `startSession` / `stopSession` / `sendMessage` / `fetchSessions`).
 */
import { ref, type Ref } from "vue";

import { apiJson, ApiError } from "@/api";
import { useToastStore } from "@/stores/toast";
import { useCodingSession } from "./useCodingSession";
import type { CodingSession, CodingMessage } from "./useCodingSession";

export type OpenCodeSession = CodingSession;
export type OpenCodeMessage = CodingMessage;

/** A selectable provider from `GET /api/oc/health`. */
export interface OcProvider {
  id: string;
  name: string;
  available: boolean;
}

/** A selectable model from `GET /api/oc/health`. */
export interface OcModel {
  id: string;
  name: string;
  provider_id: string;
}

interface OcHealthResponse {
  provider?: string;
  available?: boolean;
  available_providers?: string[];
  providers?: OcProvider[];
  models?: OcModel[];
}

interface OcRevertResponse {
  ok: boolean;
  session_id: string;
  removed?: number;
  remaining?: number;
}

// Module-level singletons so the OC pill, ChatView, and ChatViewOpenCode
// all share the same provider/model candidate lists and selection state.
const _providers: Ref<OcProvider[]> = ref([]);
const _models: Ref<OcModel[]> = ref([]);
const _selectedModelId: Ref<string> = ref("");
const _healthLoaded = ref(false);

function toastError(msg: string): void {
  useToastStore().push({
    id: crypto.randomUUID(),
    kind: "error",
    message: msg,
    timeoutMs: 5000,
  });
}

export function useOpenCode() {
  const core = useCodingSession("oc");

  /**
   * Fetch the OC provider/model candidate lists via `GET /api/oc/health`.
   * `models[]` may be empty (backend `available_models()` not yet wired) —
   * the dropdown then shows the placeholder option only, but stays present.
   */
  async function fetchHealth(): Promise<void> {
    try {
      const res = await apiJson<OcHealthResponse>("GET", "/api/oc/health");
      _providers.value = Array.isArray(res.providers) ? res.providers : [];
      _models.value = Array.isArray(res.models) ? res.models : [];
      _healthLoaded.value = true;
    } catch {
      // Health is best-effort; keep whatever we had. Dropdown still renders.
      _providers.value = [];
      _models.value = [];
      _healthLoaded.value = true;
    }
  }

  /**
   * Persist the chosen model as the OC global default via
   * `PUT /api/oc/config {model_id, provider_id?}` (零后端改动). Takes effect
   * on the next message. `model_id === ""` clears the override (falls back
   * to whatever config holds). Mirrors `currentModel` for the toolbar label.
   */
  async function selectModel(modelId: string): Promise<void> {
    const provider = _models.value.find((m) => m.id === modelId);
    const config: Record<string, unknown> = { model_id: modelId };
    if (provider !== undefined) config["provider_id"] = provider.provider_id;
    try {
      await apiJson("PUT", "/api/oc/config", { config });
      _selectedModelId.value = modelId;
      core.currentModel.value = modelId;
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : "Failed to select model");
    }
  }

  /**
   * Revert the session to before `messageId`, removing that message and all
   * later ones. Backend `RevertRequest{message_id?|after_index?,part_id?}`.
   * On success the local message bucket is reloaded from `fetchSessions`'
   * context refresh path; we optimistically drop messages at/after the id.
   */
  async function revert(
    sessionId: string,
    messageId: string,
    partId?: string,
  ): Promise<void> {
    const body: Record<string, unknown> = { message_id: messageId };
    if (partId !== undefined) body["part_id"] = partId;
    try {
      const res = await apiJson<OcRevertResponse>(
        "POST",
        `/api/oc/sessions/${sessionId}/revert`,
        body,
      );
      if (res.ok) {
        // Refresh context badge after the server-side revert.
        await core.refreshContextUsage(sessionId);
      }
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : "Revert failed");
    }
  }

  return {
    // ── legacy-compatible surface ──────────────────────────────────────────
    isOCMode: core.isMode,
    sessions: core.sessions,
    activeSessionId: core.activeSessionId,
    activeSession: core.activeSession,
    messages: core.messages,
    loading: core.loading,
    streaming: core.streaming,
    startSession: core.startSession,
    quickNewSession: core.quickNewSession,
    stopSession: core.stopSession,
    deleteSession: core.deleteSession,
    sendMessage: core.sendMessage,
    fetchSessions: core.fetchSessions,
    // ── shared rich surface ─────────────────────────────────────────────────
    setActive: core.setActive,
    currentModel: core.currentModel,
    fetchCurrentModel: core.fetchCurrentModel,
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
    // ── queue / progress (shared core) ───────────────────────────────────────
    queue: core.queue,
    removeFromQueue: core.removeFromQueue,
    activeProgress: core.activeProgress,
    progressFor: core.progressFor,
    // ── history / restore / channel notify (shared core) ─────────────────────
    historySessions: core.historySessions,
    historyLoading: core.historyLoading,
    loadHistorySessions: core.loadHistorySessions,
    loadSessionHistory: core.loadSessionHistory,
    restoreSession: core.restoreSession,
    setWechatNotify: core.setWechatNotify,
    setFeishuNotify: core.setFeishuNotify,
    // ── mode entry/exit (raw names for pill + ChatView) ──────────────────────
    enterMode: core.enterMode,
    exitMode: core.exitMode,
    enterOCMode: core.enterMode,
    exitOCMode: core.exitMode,
    // ── floating panel visibility (V1 ocPanelOpen) ───────────────────────────
    panelOpen: core.panelOpen,
    togglePanel: core.togglePanel,
    collapsePanel: core.collapsePanel,
    // ── OC-specific (功能块 7 差异点) ─────────────────────────────────────────
    providers: _providers,
    models: _models,
    selectedModelId: _selectedModelId,
    fetchHealth,
    selectModel,
    revert,
  };
}
