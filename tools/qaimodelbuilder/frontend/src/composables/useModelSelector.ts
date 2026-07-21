// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useModelSelector` — composable for fetching available chat models.
 *
 * Provides reactive model list + loading state for the chat model
 * selector dropdown. This fetches LOCAL on-device models only
 * (`GET /api/service/models`); cloud models are fetched
 * separately by the dropdown via `/api/model-catalog/cloud-models`.
 */
import { ref, computed, type Ref, type ComputedRef } from "vue";
import { apiJson } from "@/api";

// ─── Types ───────────────────────────────────────────────────────────────────

export interface ModelInfo {
  id?: string;
  /** Some backend variants surface model id under `model_id`. */
  model_id?: string;
  name?: string;
  provider?: string;
  /**
   * V1 parity (index.html:1025) — local models render the same `formatCtx()`
   * badge as cloud entries when the backend ever populates this. The current
   * V2 `/api/service/models` payload does not include it (domain entity in
   * `qai.model_runtime.domain.entities` only carries name / size_mb /
   * model_format), so the dropdown's `v-if="formatCtx(...) !== ''"` guard
   * keeps the badge hidden until the backend adds the field. Optional here
   * to match cloud's existing field shape and avoid template `as any` casts.
   */
  context_length?: number;
  /**
   * V1 parity (`/api/models` per-entry `is_running` → ● Running dot). The V2
   * `/api/service/models` payload now tail-appends this (backend compares the
   * running daemon's loaded model to each entry). Optional so older payloads
   * (and cloud entries) without it fall back to status-derived detection.
   */
  is_running?: boolean;
  [key: string]: unknown;
}

interface ModelsResponse {
  models: ModelInfo[];
}

// ─── Composable ──────────────────────────────────────────────────────────────

export function useModelSelector() {
  const models: Ref<ModelInfo[]> = ref([]);
  const loading: Ref<boolean> = ref(false);
  const error: Ref<string | null> = ref(null);

  const hasModels: ComputedRef<boolean> = computed(
    () => models.value.length > 0,
  );

  async function fetchModels(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const res = await apiJson<ModelsResponse>("GET", "/api/service/models");
      models.value = res.models ?? [];
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Failed to load models";
      models.value = [];
    } finally {
      loading.value = false;
    }
  }

  function getModelLabel(model: ModelInfo): string {
    return model.name ?? model.id ?? model.model_id ?? "Unknown Model";
  }

  function getModelId(model: ModelInfo): string {
    // V1 convention (backend ``main.py:163`` builds ``model_id =
    // f"local::{id}"`` for on-device models): local model ids carry a
    // ``local::`` prefix end-to-end. The chat backend's resolver routes a
    // ``local::``-prefixed turn to the running local Genie service and
    // strips the prefix for the daemon. ``/api/service/models`` (this
    // composable's only source) returns on-device entries with just a
    // ``name`` and no ``id``/``model_id``, so we synthesise the prefixed id
    // here. When the backend already supplies a prefixed id we pass it
    // through unchanged.
    const raw = model.id ?? model.model_id ?? model.name ?? "unknown";
    return raw.includes("::") ? raw : `local::${raw}`;
  }

  return {
    models,
    loading,
    error,
    hasModels,
    fetchModels,
    getModelLabel,
    getModelId,
  };
}
