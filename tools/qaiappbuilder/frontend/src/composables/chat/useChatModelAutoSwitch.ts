// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useChatModelAutoSwitch` — V1 `useModels.js:219-329` parity.
 *
 * After each model-list refresh, decide whether the currently-selected chat
 * model needs to be auto-switched away (because it is a LOCAL model that is no
 * longer running) and keep a `loadingLocalModelId` guard so a model the user
 * just picked + triggered auto-load is NOT yanked out from under them.
 *
 * Behaviour (the service is always launched on this machine):
 *
 * - Clear `loadingLocalModelId` once that model reports running, or once no
 *   local model is running at all (load failed).
 * - If the current selection is a LOCAL model with `is_running === false`,
 *   is NOT the model currently loading, and there is a running local model
 *   to fall back to, switch the selection to that running model and persist
 *   it (V1 `/api/preferences`).
 *
 * The WeChat-channel coupling in V1 is intentionally NOT reproduced here: V2
 * channels are single-instance with their own model selector (AGENTS.md
 * §3.9.1), so chat-model auto-switch is a chat-only concern.
 */
import { ref, type Ref } from "vue";

import { saveChatModelPreference } from "@/composables/chat/useChatModelPreference";

/** Minimal shape this composable needs from a model-list entry. */
export interface AutoSwitchModel {
  model_id: string;
  provider?: string;
  is_local?: boolean;
  is_running?: boolean;
  is_placeholder?: boolean;
}

export interface UseChatModelAutoSwitchOptions {
  /** Current selection getter (the active tab's model id + provider). */
  selection: () => { id: string; provider: string };
  /**
   * Apply a new selection (patch the active tab). Persistence to
   * `/api/preferences` is handled inside the composable (V1 parity).
   */
  applySelection: (id: string, provider: string) => void;
}

export function useChatModelAutoSwitch(options: UseChatModelAutoSwitchOptions) {
  /** Id of a local model the user just picked + triggered auto-load for. */
  const loadingLocalModelId = ref<string>("");

  /**
   * V1 `autoSwitchStoppedModel`: run after every model-list refresh.
   * `models` must carry the backend-authoritative `is_running` flag.
   */
  function autoSwitchStoppedModel(models: AutoSwitchModel[]): void {
    if (!models.length) return;

    const firstRunning = models.find(
      (m) => m.is_local && m.is_running && !m.is_placeholder,
    );

    // Clear the loading guard once the loading model is running, or once no
    // local model is running at all (load failed).
    if (loadingLocalModelId.value) {
      const loadingModel = models.find(
        (m) => m.model_id === loadingLocalModelId.value,
      );
      const anyLocalRunning = models.some(
        (m) => m.is_local && m.is_running && !m.is_placeholder,
      );
      if ((loadingModel && loadingModel.is_running) || !anyLocalRunning) {
        loadingLocalModelId.value = "";
      }
    }

    const sel = options.selection();
    const current = models.find(
      (m) =>
        m.model_id === sel.id &&
        (sel.provider === "" || m.provider === sel.provider),
    );
    if (
      current &&
      current.is_local &&
      current.is_running === false &&
      current.model_id !== loadingLocalModelId.value &&
      firstRunning
    ) {
      options.applySelection(firstRunning.model_id, firstRunning.provider ?? "");
      saveChatModelPreference(firstRunning.model_id, firstRunning.provider ?? "");
    }
  }

  return {
    loadingLocalModelId,
    autoSwitchStoppedModel,
  };
}

/** Re-exported for callers that hold their own model list ref. */
export type AutoSwitchModelList = Ref<AutoSwitchModel[]>;
