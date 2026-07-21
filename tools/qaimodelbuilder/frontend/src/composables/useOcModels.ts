// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useOcModels — V1-parity OpenCode model-selection composable.
 *
 * Extracted from `OpenCodeConfigPanel.vue` (cohesion split, mirrors the
 * `useCcModels` pattern). Owns the model data-flow: cloud-catalog fetch
 * (`loadCloudModels`) → `availableModels` → `selectedModelKey` /
 * `visibleModels`, plus the "go to Cloud Models settings" navigation.
 *
 * The composable reads/writes a small slice of the host's reactive
 * `OcConfig` (`use_cloud_models` / `model_id` / `provider_id` /
 * `model_list`). No watch, no lifecycle hooks: pure ref + computed + one
 * async fetch. The host keeps `onMounted(() => void loadCloudModels())`.
 */
import { computed, ref, type ComputedRef, type Ref, type WritableComputedRef } from "vue";
import { useRouter } from "vue-router";
import { fetchCloudModels, fetchCloudProviders } from "@/api";
import type { CloudModelEntry, CloudProviderMeta } from "@/types/cloudModels";

/** Per-entry shape of the host config's custom `model_list`. */
export interface OcModelListEntry {
  uniqueKey?: string;
  id: string;
  label?: string;
  provider?: string;
  desc?: string;
}

/** A single selectable model option (cloud-catalog or built-in/custom). */
export interface ModelOption {
  uniqueKey: string;
  id: string;
  provider: string;
  label: string;
}

/** Minimal slice of the host's `OcConfig` this composable touches. */
export interface OcModelsConfigShape {
  use_cloud_models: boolean;
  model_id: string;
  provider_id: string;
  model_list: OcModelListEntry[];
}

export interface UseOcModelsReturn {
  cloudModels: Ref<CloudModelEntry[]>;
  cloudProviders: Ref<Record<string, CloudProviderMeta>>;
  availableModels: ComputedRef<ModelOption[]>;
  selectedModelKey: WritableComputedRef<string>;
  modelsExpanded: Ref<boolean>;
  visibleModels: ComputedRef<ModelOption[]>;
  loadCloudModels: () => Promise<void>;
  navigateToCloudModels: () => void;
}

// V1 parity (OpenCodeConfigPanel.js:222-226): built-in fallback list shown
// when use_cloud_models=false and no custom model_list is configured.
// TODO: These should be loaded from config rather than hardcoded.
const BUILTIN_OC_MODELS: ModelOption[] = [
  { uniqueKey: "cloud::claude-sonnet", id: "claude-sonnet", provider: "anthropic", label: "Claude Sonnet" },
  { uniqueKey: "openai::gpt-4o", id: "gpt-4o", provider: "openai", label: "GPT-4o" },
  { uniqueKey: "openai::gpt-4o-mini", id: "gpt-4o-mini", provider: "openai", label: "GPT-4o Mini" },
];

export function useOcModels<T extends OcModelsConfigShape>(opts: {
  /** Reactive config object — read/written for model selection. */
  cfg: T;
}): UseOcModelsReturn {
  const { cfg } = opts;
  const router = useRouter();

  const cloudModels = ref<CloudModelEntry[]>([]);
  const cloudProviders = ref<Record<string, CloudProviderMeta>>({});

  const availableModels = computed<ModelOption[]>(() => {
    if (cfg.use_cloud_models) {
      const pinned: CloudModelEntry[] = [];
      const rest: CloudModelEntry[] = [];
      for (const m of cloudModels.value) {
        if (cloudProviders.value[m.provider]?.pinned) pinned.push(m);
        else rest.push(m);
      }
      return [...pinned, ...rest].map((m) => ({
        uniqueKey: `${m.provider}::${m.model_id}`,
        id: m.model_id,
        provider: (m.provider ?? "").toLowerCase(),
        label: `${m.name} (${m.provider})`,
      }));
    }
    // use_cloud_models=false: custom model_list if present, else built-in list
    // (V1 parity OpenCodeConfigPanel.js:213-226).
    if (cfg.model_list.length > 0) {
      return cfg.model_list
        .filter((m) => typeof m.id === "string" && m.id)
        .map((m) => ({
          uniqueKey: m.uniqueKey || `${m.provider ?? ""}::${m.id}`,
          id: m.id,
          provider: (m.provider ?? "").toLowerCase(),
          label: m.label || m.id,
        }));
    }
    return BUILTIN_OC_MODELS;
  });

  const selectedModelKey = computed<string>({
    get() {
      const exact = availableModels.value.find(
        (m) => m.id === cfg.model_id && m.provider === (cfg.provider_id ?? "").toLowerCase(),
      );
      if (exact) return exact.uniqueKey;
      const fb = availableModels.value.find((m) => m.id === cfg.model_id);
      return fb ? fb.uniqueKey : "";
    },
    set(key: string) {
      const m = availableModels.value.find((x) => x.uniqueKey === key);
      if (m) {
        cfg.model_id = m.id;
        cfg.provider_id = m.provider || cfg.provider_id;
      }
    },
  });

  const modelsExpanded = ref(false);
  const visibleModels = computed<ModelOption[]>(() => {
    const all = availableModels.value;
    if (modelsExpanded.value || all.length <= 3) return all;
    const top = all.slice(0, 3);
    const key = selectedModelKey.value;
    if (key && !top.some((m) => m.uniqueKey === key)) {
      const sel = all.find((m) => m.uniqueKey === key);
      if (sel) return [...top, sel];
    }
    return top;
  });

  async function loadCloudModels(): Promise<void> {
    try {
      const [m, p] = await Promise.all([fetchCloudModels(), fetchCloudProviders()]);
      cloudModels.value = Array.isArray(m?.models) ? m.models : [];
      cloudProviders.value = p?.providers && typeof p.providers === "object" ? p.providers : {};
    } catch { /* non-fatal */ }
  }

  // ─── Navigate to Cloud Models (V1 parity OpenCodeConfigPanel.js:484) ─────────
  // V1: ctx.navigateTo('settings','cloud-models'). V2 uses vue-router to land on
  // the Settings page's Cloud Models tab (?tab=cloud-models) — never a native
  // navigation (AGENTS.md §3.9).
  function navigateToCloudModels(): void {
    void router.push({ path: "/settings", query: { tab: "cloud-models" } });
  }

  return {
    cloudModels,
    cloudProviders,
    availableModels,
    selectedModelKey,
    modelsExpanded,
    visibleModels,
    loadCloudModels,
    navigateToCloudModels,
  };
}
