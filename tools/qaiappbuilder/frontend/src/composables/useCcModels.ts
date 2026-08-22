// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useCcModels — V1-parity Claude Code health + model-list composable.
 *
 * Extracted from `ClaudeCodeConfigPanel.vue` (cohesion split). Owns the
 * single data-flow: `checkHealth()` → `health` → `modelOptions` →
 * `displayedModels` / `modelSourceBadge`. Together with `useCcAuth` this
 * gives the panel two clean composable layers (auth/credentials +
 * health/models) and a thin host that wires them together.
 *
 * **Lifecycle contract**: the host keeps `onMounted(() => { void
 * loadConfig().then(loadCredentials); void checkHealth(); })` so the
 * timing is byte-for-byte identical to the inline implementation.
 * `checkHealth` is also passed to `useCcAuth` as `onAfterMutation` so
 * credential saves / removes refresh the health badge automatically.
 *
 * No watch, no lifecycle hooks inside this composable: pure ref +
 * computed + one async fetch. Safe to call inside any setup function.
 */
import { computed, ref, type ComputedRef, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { fetchCcHealth } from "@/api";
import type { CodingHealthResponse } from "@/api/aiCodingHealth";

/** Minimal slice of the host's `CcConfig` we read. */
export interface CcModelsConfigShape {
  model: string;
}

/** V1 4-state model-source badge (upstream / cache / fallback-no-key / fallback-error). */
export interface ModelSourceBadge {
  source: string;
  icon: string;
  color: string;
  bg: string;
  label: string;
  title: string;
}

export interface UseCcModelsReturn {
  health: Ref<CodingHealthResponse | null>;
  healthLoading: Ref<boolean>;
  checkHealth: (force?: boolean) => Promise<void>;
  modelOptions: ComputedRef<{ id: string; label: string }[]>;
  MODELS_COLLAPSED_LIMIT: number;
  modelsExpanded: Ref<boolean>;
  displayedModels: ComputedRef<{ id: string; label: string }[]>;
  hiddenModelCount: ComputedRef<number>;
  modelSourceBadge: ComputedRef<ModelSourceBadge | null>;
}

export function useCcModels<T extends CcModelsConfigShape>(opts: {
  /** Reactive config object — composable reads `cfg.model`. */
  cfg: T;
}): UseCcModelsReturn {
  const { t } = useI18n();
  const { cfg } = opts;

  const health = ref<CodingHealthResponse | null>(null);
  const healthLoading = ref(false);

  async function checkHealth(force = false): Promise<void> {
    healthLoading.value = true;
    try {
      health.value = await fetchCcHealth(force ? { refresh: true } : undefined);
    } catch {
      health.value = null;
    } finally {
      healthLoading.value = false;
    }
  }

  // ─── Model options (from health.models[], plus current value) ─────────────
  const modelOptions = computed<{ id: string; label: string }[]>(() => {
    const opts: { id: string; label: string }[] = [];
    const seen = new Set<string>();
    // health.models[] items are untyped dicts in the OpenAPI schema; the
    // backend emits `{ id, name }` per entry (V1 parity), so coerce here.
    const models = (health.value?.models ?? []) as Array<{
      id?: string;
      name?: string;
    }>;
    for (const m of models) {
      if (!m.id || seen.has(m.id)) continue;
      seen.add(m.id);
      opts.push({
        id: m.id,
        label:
          m.name && m.name !== m.id ? `${m.name} (${m.id})` : m.id,
      });
    }
    if (cfg.model && !seen.has(cfg.model)) {
      opts.unshift({ id: cfg.model, label: cfg.model });
    }
    return opts;
  });

  // Model radio list collapse (V1 parity): show the first few + the current
  // selection by default; "Show N more" expands the full list.
  // V1 parity (ClaudeCodeConfigPanel.js:220): MODELS_COLLAPSED_LIMIT = 3
  // (matches the OC panel's collapse threshold in useOcModels too).
  const MODELS_COLLAPSED_LIMIT = 3;
  const modelsExpanded = ref(false);
  const displayedModels = computed<{ id: string; label: string }[]>(() => {
    const all = modelOptions.value;
    if (modelsExpanded.value || all.length <= MODELS_COLLAPSED_LIMIT) {
      return all;
    }
    const head = all.slice(0, MODELS_COLLAPSED_LIMIT);
    // Always keep the currently-selected model visible even when collapsed.
    if (cfg.model && !head.some((m) => m.id === cfg.model)) {
      const cur = all.find((m) => m.id === cfg.model);
      if (cur) head.push(cur);
    }
    return head;
  });
  const hiddenModelCount = computed(() =>
    Math.max(0, modelOptions.value.length - MODELS_COLLAPSED_LIMIT),
  );

  // ─── Model source badge (V1 /api/cc/models parity, folded into health) ────
  // V1 4-state `source`: upstream (✓ live) / cache (⚡ <5min) / fallback-no-key
  // (⚠ no key) / fallback-error (✗ upstream unavailable). The backend strips
  // the base_url to scheme://netloc so an embedded gateway token never leaks.
  const modelSourceBadge = computed<ModelSourceBadge | null>(() => {
    const src = health.value?.models_source;
    if (!src) return null;
    switch (src) {
      case "upstream":
        return {
          source: src,
          icon: "✓",
          color: "#4ade80",
          bg: "rgba(74,222,128,.12)",
          label: t("claudeCode.config.modelsSourceUpstream", "Upstream"),
          title: t(
            "claudeCode.config.modelsSourceUpstreamTitle",
            "Live model list fetched from the upstream service",
          ),
        };
      case "cache":
        return {
          source: src,
          icon: "⚡",
          color: "#7eb8f7",
          bg: "rgba(126,184,247,.12)",
          label: t("claudeCode.config.modelsSourceCache", "Cached"),
          title: t(
            "claudeCode.config.modelsSourceCacheTitle",
            "Using the most recent cached fetch (within 5 minutes)",
          ),
        };
      case "fallback-no-key":
        return {
          source: src,
          icon: "⚠",
          color: "#fbbf24",
          bg: "rgba(251,191,36,.12)",
          label: t(
            "claudeCode.config.modelsSourceNoKey",
            "Local fallback (no API key)",
          ),
          title: t(
            "claudeCode.config.modelsSourceNoKeyTitle",
            "No API key or Auth Token configured, cannot call upstream /v1/models; showing model_list from forge_config.json as fallback",
          ),
        };
      case "fallback-error":
        return {
          source: src,
          icon: "✕",
          color: "#f87171",
          bg: "rgba(248,113,113,.12)",
          label: t(
            "claudeCode.config.modelsSourceError",
            "Local fallback (upstream unavailable)",
          ),
          title: t(
            "claudeCode.config.modelsSourceErrorTitle",
            "Upstream /v1/models call failed; fell back to model_list from forge_config.json",
          ),
        };
      default:
        return null;
    }
  });

  return {
    health,
    healthLoading,
    checkHealth,
    modelOptions,
    MODELS_COLLAPSED_LIMIT,
    modelsExpanded,
    displayedModels,
    hiddenModelCount,
    modelSourceBadge,
  };
}
