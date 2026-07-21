// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useAssistantLabel` — resolve the display name shown in an assistant
 * message bubble's header (V1 parity).
 *
 * Replicates V1 `useModels.js:53-70 msgModelName(msg)`:
 *   1. message carries a `model_id` → look it up in the available model
 *      lists (local on-device + cloud catalog) by id (+ optional provider)
 *      and show its display name;
 *   2. id present but not in either list (model went offline) → show the
 *      bare id with any `local::` / `provider::` prefix stripped;
 *   3. no `model_id` (legacy rows / mid-stream) → fall back to the
 *      currently-selected model's name, then finally to the generic
 *      "Assistant" i18n label.
 *
 * The model lists come from the same endpoints the chat model dropdown
 * uses (`/api/service/models` for local, `/api/model-catalog/cloud-models`
 * for cloud). We keep this in its own composable — instead of inlining the
 * lookup into the already-large ChatMessageList component — so the
 * resolution logic stays a single-responsibility, testable unit.
 */
import { ref, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson } from "@/api";

interface LocalModel {
  id?: string;
  model_id?: string;
  name?: string;
  provider?: string;
}

interface ModelEntry {
  /** Canonical model id (e.g. "local::foo" / "claude-4-6-sonnet"). */
  id: string;
  /** Display name. */
  name: string;
  /** Provider slug ("" for local / unknown). */
  provider: string;
}

/** Strip a leading `local::` / `provider::` prefix, V1 useModels.js:65. */
function stripPrefix(id: string): string {
  return id.includes("::") ? id.split("::").slice(1).join("::") : id;
}

export function useAssistantLabel() {
  const { t } = useI18n();

  // Flat list of all known models (local + cloud), id-indexed lookups
  // walk this array so a provider filter can disambiguate cloud entries
  // that share a `model_id` (V1 useModels.js:58-61).
  const entries: Ref<ModelEntry[]> = ref([]);
  const loaded = ref(false);
  const loading = ref(false);

  async function ensureLoaded(): Promise<void> {
    if (loaded.value || loading.value) return;
    loading.value = true;
    const acc: ModelEntry[] = [];
    // Local on-device models.
    try {
      const res = await apiJson<{ models?: LocalModel[] }>(
        "GET",
        "/api/service/models",
      );
      for (const m of res.models ?? []) {
        const id = m.id ?? m.model_id;
        if (id !== undefined && id !== "") {
          acc.push({
            id,
            name: m.name ?? id,
            provider: m.provider ?? "",
          });
        }
      }
    } catch {
      /* local service offline — cloud entries may still resolve */
    }
    // Cloud catalog models.
    try {
      const res = await apiJson<{ models?: Array<Record<string, unknown>> }>(
        "GET",
        "/api/model-catalog/cloud-models",
      );
      for (const m of res.models ?? []) {
        const id = (m.model_id ?? m.id) as string | undefined;
        if (id !== undefined && id !== "") {
          acc.push({
            id,
            name: (m.name as string | undefined) ?? id,
            provider: (m.provider as string | undefined) ?? "",
          });
        }
      }
    } catch {
      /* cloud catalog unavailable */
    }
    entries.value = acc;
    loaded.value = true;
    loading.value = false;
  }

  /** Look up a display name for `(modelId, modelProvider)`; null if absent. */
  function lookupName(
    modelId: string,
    modelProvider?: string,
  ): string | null {
    const match = entries.value.find(
      (m) =>
        m.id === modelId &&
        (modelProvider === undefined ||
          modelProvider === "" ||
          m.provider === modelProvider),
    );
    return match?.name ?? null;
  }

  /**
   * V1 `msgModelName` parity. `fallbackId` / `fallbackProvider` carry the
   * currently-selected model so legacy / mid-stream messages (no own
   * `modelId`) still show a sensible name (V1 useModels.js:68-69).
   */
  function resolveAssistantLabel(
    modelId: string | undefined,
    modelProvider: string | undefined,
    fallbackId?: string,
    fallbackProvider?: string,
  ): string {
    if (modelId !== undefined && modelId !== "") {
      const name = lookupName(modelId, modelProvider);
      return name ?? stripPrefix(modelId);
    }
    if (
      fallbackId !== undefined &&
      fallbackId !== "" &&
      fallbackId !== "qai-default"
    ) {
      const name = lookupName(fallbackId, fallbackProvider);
      return name ?? stripPrefix(fallbackId);
    }
    return t("chat.assistant");
  }

  return {
    entries,
    loaded,
    loading,
    ensureLoaded,
    lookupName,
    resolveAssistantLabel,
  };
}
