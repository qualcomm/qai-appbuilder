// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useChatModelPreference` — persist + restore the globally selected chat
 * model (V1 `useModels.js:13` `selectedModelId` is a single global ref that
 * is saved to `/api/preferences` and restored on load).
 *
 * V1 parity:
 * - `useModels.js:111-115` — selecting a model `POST`s
 *   `{selected_model_id, selected_model_provider}` to `/api/preferences`.
 * - `useModels.js:182-195` — on load, the saved id/provider are read back and
 *   used as the active selection (instead of silently falling back to the
 *   first cloud model).
 *
 * V2 stores the active model per-tab (`chatTabs`), but the *default* a fresh
 * tab adopts must come from this persisted global preference — otherwise a
 * page refresh / new session loses the user's last (e.g. local) model and the
 * `ChatComposer` auto-select watcher snaps back to the first cloud model
 * (audit Bug #3 / difference #2).
 */
import { apiJson } from "@/api";

export interface ChatModelPreference {
  selected_model_id: string;
  selected_model_provider: string;
}

interface PreferencesResponse {
  selected_model_id?: string;
  selected_model_provider?: string;
}

/**
 * Read the persisted global chat-model selection.  Never throws — returns
 * empty strings on any failure so callers can fall back to their own
 * defaulting logic.
 */
export async function loadChatModelPreference(): Promise<ChatModelPreference> {
  const prefs = await apiJson<PreferencesResponse>(
    "GET",
    "/api/preferences",
  ).catch(() => ({}) as PreferencesResponse);
  return {
    selected_model_id: prefs.selected_model_id ?? "",
    selected_model_provider: prefs.selected_model_provider ?? "",
  };
}

/**
 * Persist the global chat-model selection (fire-and-forget, V1 parity).
 *
 * Mirrors V1 `useModels.js:111-115`: both id and provider are sent so the
 * dropdown ✓ disambiguates two cloud entries that share a `model_id`.  An
 * empty `modelId` clears the preference ("follow global default").
 */
export function saveChatModelPreference(
  modelId: string,
  modelProvider: string,
): void {
  void apiJson("POST", "/api/preferences", {
    selected_model_id: modelId,
    selected_model_provider: modelProvider,
  }).catch(() => {
    // Non-fatal: a transient persistence failure must never break model
    // switching (V1 also ignores the POST result).
  });
}
