// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useCloudModelNames — V1-parity cloud-model id ↔ display-name index for
 * the Chat composer.
 *
 * Extracted from `ChatComposer.vue` (cohesion split). The composable
 * fetches `/api/model-catalog/cloud-models` once on demand and exposes:
 *
 *   • `cloudModelMap`     — `{ model_id: name }` lookup, used by the
 *                           chip label so a selected cloud model shows
 *                           its display name (V1 useModels.js parity).
 *   • `cloudModelEntries` — flat `[{ id, provider }]` list preserving
 *                           the FIRST entry's provider when multiple
 *                           cloud entries share the same `model_id`
 *                           (used by the composer's auto-select watch
 *                           to seed `tab.modelProvider` correctly).
 *   • `loadCloudModelNames()` — caller-controlled fetch trigger so the
 *                               existing `onMounted(() => void load…())`
 *                               timing in ChatComposer is preserved
 *                               byte-for-byte.
 *
 * The composable does **not** auto-fetch on mount, hold any watchers,
 * or own a streaming/RAF lifecycle. Pure ref + async fetch — safe to
 * call inside any setup function and trivial to re-use elsewhere.
 */
import { ref, type Ref } from "vue";
import { apiJson } from "@/api";

export interface UseCloudModelNamesReturn {
  cloudModelMap: Ref<Record<string, string>>;
  cloudModelEntries: Ref<Array<{ id: string; provider: string }>>;
  /**
   * `true` once `loadCloudModelNames()` has settled (success OR failure), so
   * callers can distinguish "cloud list confirmed empty" from "cloud list not
   * fetched yet". The default-model auto-select needs this to avoid prematurely
   * seeding a LOCAL model while the cloud fetch is still in flight (which would
   * make the tab look like it defaulted to an on-device model).
   */
  cloudModelsLoaded: Ref<boolean>;
  loadCloudModelNames: () => Promise<void>;
}

export function useCloudModelNames(): UseCloudModelNamesReturn {
  // Cloud model id → display name map, used ONLY by the chip label so a
  // selected cloud model shows its name (V1 parity). Kept separate from
  // `useModelSelector` (which the dropdown shares) so the dropdown's
  // local/cloud separation is untouched. Source is the same endpoint the
  // dropdown reads its cloud entries from.
  const cloudModelMap = ref<Record<string, string>>({});
  // V1 parity (useModels.js:30) — same source, but indexed as a list so we
  // keep the FIRST entry's provider when multiple cloud entries share the
  // same `model_id`. Used by the auto-select watch below to seed
  // `tab.modelProvider` (otherwise the dropdown ✓ would mark every entry
  // that happens to share `model_id`).
  const cloudModelEntries = ref<Array<{ id: string; provider: string }>>([]);
  // Whether the cloud-model fetch has settled at least once (success/failure).
  const cloudModelsLoaded = ref<boolean>(false);

  async function loadCloudModelNames(): Promise<void> {
    try {
      const res = await apiJson<{ models?: Array<Record<string, unknown>> }>(
        "GET",
        "/api/model-catalog/cloud-models",
      );
      const map: Record<string, string> = {};
      const entries: Array<{ id: string; provider: string }> = [];
      for (const m of res.models ?? []) {
        const id = (m.model_id ?? m.id) as string | undefined;
        if (id !== undefined && id !== "") {
          // Keep the FIRST seen name for the chip label (older behaviour).
          if (!(id in map)) {
            map[id] = (m.name as string | undefined) ?? id;
          }
          entries.push({
            id,
            provider: (m.provider as string | undefined) ?? "",
          });
        }
      }
      cloudModelMap.value = map;
      cloudModelEntries.value = entries;
    } catch {
      cloudModelMap.value = {};
      cloudModelEntries.value = [];
    } finally {
      cloudModelsLoaded.value = true;
    }
  }

  return {
    cloudModelMap,
    cloudModelEntries,
    cloudModelsLoaded,
    loadCloudModelNames,
  };
}
