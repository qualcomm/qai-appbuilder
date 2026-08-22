// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useCloudModelOptions` — shared cloud-model dropdown source.
 *
 * Extracted from AgentRoleForm's inline cloud-model loading so the THREE
 * discussion surfaces that need a cloud-model `<select>` share ONE field set
 * and ONE filter rule (AGENTS.md 细则 2：复用 > 重造 / §6.2):
 *
 *   - AgentRoleForm.vue       (role model — required)
 *   - ModeEditorSection.vue   (mode "system model" — required)
 *   - DiscussionPanel.vue     (session-level classifier / planner — optional)
 *
 * Local models are excluded verbatim (`is_local !== true`) — discussions run
 * exclusively on cloud models. Each caller instantiates its own copy (no
 * module-level mutable state → no stale-closure / global-ref anti-pattern);
 * the fetch is de-duplicated per instance via `cloudModelsLoaded`.
 *
 * `modelMissing(id)` lets a host render a legacy fall-back `<option>` so a role
 * that points at a since-deleted model is NOT silently cleared.
 */
import { computed, ref, type ComputedRef, type Ref } from "vue";

import { fetchCloudModels } from "@/api/cloudModels";
import type { CloudModelEntry } from "@/types/cloudModels";

export interface UseCloudModelOptions {
  /** Reactive raw catalog (cloud + local, as returned by the backend). */
  readonly cloudModels: Ref<CloudModelEntry[]>;
  /** Reactive: a fetch has completed at least once. */
  readonly cloudModelsLoaded: Ref<boolean>;
  /** Cloud-only options (local models filtered out). */
  readonly cloudModelOptions: ComputedRef<CloudModelEntry[]>;
  /** "provider · name" label for an option (falls back to model_id). */
  cloudModelLabel(m: CloudModelEntry): string;
  /** Fetch the catalog once (de-duplicated; failures → empty list). */
  loadCloudModels(): Promise<void>;
  /** True when `id` is non-empty but not present in the cloud options (so the
   *  host can render a legacy fall-back option rather than dropping the value). */
  modelMissing(id: string): boolean;
}

export function useCloudModelOptions(): UseCloudModelOptions {
  const cloudModels = ref<CloudModelEntry[]>([]);
  const cloudModelsLoaded = ref(false);

  const cloudModelOptions = computed<CloudModelEntry[]>(() =>
    cloudModels.value.filter((m) => m.is_local !== true),
  );

  function cloudModelLabel(m: CloudModelEntry): string {
    const name = m.name?.trim() || m.model_id;
    return m.provider ? `${m.provider} \u00b7 ${name}` : name;
  }

  function modelMissing(id: string): boolean {
    return id !== "" && !cloudModelOptions.value.some((m) => m.model_id === id);
  }

  async function loadCloudModels(): Promise<void> {
    if (cloudModelsLoaded.value) return;
    try {
      const res = await fetchCloudModels();
      cloudModels.value = Array.isArray(res?.models) ? res.models : [];
    } catch {
      cloudModels.value = [];
    } finally {
      cloudModelsLoaded.value = true;
    }
  }

  return {
    cloudModels,
    cloudModelsLoaded,
    cloudModelOptions,
    cloudModelLabel,
    loadCloudModels,
    modelMissing,
  };
}
