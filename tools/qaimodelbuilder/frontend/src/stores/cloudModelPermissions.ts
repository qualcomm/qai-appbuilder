// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Cloud-model permissions Pinia store.
 *
 * Reads the per-``(provider, model_id)`` permission snapshot from
 * ``GET /api/model-catalog/cloud-models/permissions`` and exposes cheap
 * lookup helpers for the chat model dropdown to filter out models the
 * current cloud API key has no access to.
 *
 * Fail-open semantics (matches the backend contract):
 *   * before the first fetch resolves        → `getStatus` returns `"unknown"`
 *     for every model → nothing hidden;
 *   * fetch fails (network / backend down)   → snapshot stays empty → same
 *     "show all" behaviour;
 *   * fetch resolves with a partial snapshot → models not listed remain
 *     `"unknown"` → visible;
 *   * only an explicit `"denied"` verdict from the backend hides a model.
 *
 * This is a deliberate UX choice: a probe failure must never make the
 * dropdown appear empty (never-preset-unavailable, PROJECT-RULES §5 /
 * State-Truth-First 铁律 1).
 *
 * The store is refreshed once at app mount (App.vue) and can be re-fetched
 * on demand via `refresh()` (no UI hook yet — reserved for later "Retry
 * scan" affordance).
 */
import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { apiJson } from "@/api";

/** Wire-form permission status. Mirrors ``PermissionStatus`` on the backend. */
export type CloudModelPermissionStatus = "unknown" | "allowed" | "denied";

interface PermissionsResponse {
  /** {provider_id: {model_id: status}} — missing entries mean "unknown". */
  permissions: Record<string, Record<string, string>>;
}

/** Recognised (typed) status values. Anything else falls back to "unknown". */
const KNOWN_STATUSES: ReadonlySet<CloudModelPermissionStatus> = new Set<
  CloudModelPermissionStatus
>(["unknown", "allowed", "denied"]);

function coerceStatus(raw: unknown): CloudModelPermissionStatus {
  if (typeof raw === "string" && KNOWN_STATUSES.has(raw as CloudModelPermissionStatus)) {
    return raw as CloudModelPermissionStatus;
  }
  return "unknown";
}

export const useCloudModelPermissionsStore = defineStore(
  "cloudModelPermissions",
  () => {
    // ─── State ────────────────────────────────────────────────────────────
    /**
     * Snapshot keyed by provider then model_id. An empty map means "no
     * information yet" — every model resolves to "unknown" (visible).
     */
    const snapshot = ref<
      Record<string, Record<string, CloudModelPermissionStatus>>
    >({});
    /** True while a fetch is in flight. Not surfaced to the dropdown (which
     *  must not flicker on refresh) but useful for tests / debug tooling. */
    const loading = ref(false);
    /** ISO timestamp of the last successful fetch (null before first). */
    const lastFetchedAt = ref<string | null>(null);

    // ─── Getters ──────────────────────────────────────────────────────────
    /**
     * Return the recorded status for ``(providerId, modelId)`` or `"unknown"`
     * when either the provider or the model has no entry in the snapshot.
     * Never returns `undefined` — callers can rely on the tri-state contract.
     */
    function getStatus(
      providerId: string | null | undefined,
      modelId: string | null | undefined,
    ): CloudModelPermissionStatus {
      if (
        providerId === null ||
        providerId === undefined ||
        providerId === "" ||
        modelId === null ||
        modelId === undefined ||
        modelId === ""
      ) {
        return "unknown";
      }
      const perModel = snapshot.value[providerId];
      if (perModel === undefined) return "unknown";
      return perModel[modelId] ?? "unknown";
    }

    /**
     * True unless the status is explicitly ``"denied"``. The dropdown uses
     * this as its "should I show this model?" gate — `"allowed"` and
     * `"unknown"` both pass (never-preset-unavailable).
     */
    function isAllowed(
      providerId: string | null | undefined,
      modelId: string | null | undefined,
    ): boolean {
      return getStatus(providerId, modelId) !== "denied";
    }

    /** True when the current snapshot carries at least one entry (any
     *  provider). Handy for tests / debug — the dropdown does not read it. */
    const hasAnyData = computed<boolean>(
      () => Object.keys(snapshot.value).length > 0,
    );

    // ─── Actions ──────────────────────────────────────────────────────────
    /**
     * Fetch the current snapshot from the backend. Fail-open: a network /
     * backend error leaves the previous snapshot untouched (so a transient
     * blip does not "unfilter" the dropdown), and if there was no previous
     * snapshot the state stays empty → every model visible.
     */
    async function refresh(): Promise<void> {
      if (loading.value) return; // dedupe overlapping refreshes
      loading.value = true;
      try {
        const res = await apiJson<PermissionsResponse>(
          "GET",
          "/api/model-catalog/cloud-models/permissions",
        );
        const parsed: Record<
          string,
          Record<string, CloudModelPermissionStatus>
        > = {};
        // Defensive parse: coerce every value through the known-set gate so
        // an unexpected future backend addition (e.g. "throttled") does not
        // widen `CloudModelPermissionStatus` accidentally.
        for (const [providerId, perModel] of Object.entries(
          res.permissions ?? {},
        )) {
          if (typeof providerId !== "string" || providerId === "") continue;
          if (perModel === null || typeof perModel !== "object") continue;
          const inner: Record<string, CloudModelPermissionStatus> = {};
          for (const [modelId, raw] of Object.entries(perModel)) {
            if (typeof modelId !== "string" || modelId === "") continue;
            inner[modelId] = coerceStatus(raw);
          }
          parsed[providerId] = inner;
        }
        snapshot.value = parsed;
        lastFetchedAt.value = new Date().toISOString();
      } catch {
        // Swallow: fail-open per the module docstring. Do NOT clear the
        // existing snapshot — a transient network blip should not "un-hide"
        // models that a previous successful scan confirmed as denied.
      } finally {
        loading.value = false;
      }
    }

    return {
      // state
      snapshot,
      loading,
      lastFetchedAt,
      // getters
      hasAnyData,
      // actions
      getStatus,
      isAllowed,
      refresh,
    };
  },
);
