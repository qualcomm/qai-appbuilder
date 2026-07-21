// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Service Pinia store.
 *
 * S5 PR-055: wraps /api/system/* routes.
 * Exposes: health status, reboot action.
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import { apiJson } from "@/api";
import type { components } from "@/types/api";

type HealthResponse = components["schemas"]["interfaces__http__routes__system__HealthResponse"];
type RebootResponse = components["schemas"]["RebootResponse"];

/**
 * `GET /api/system/edition` payload. The generated `EditionResponse` schema
 * only carries `edition`; the backend additionally returns the derived
 * `is_internal` boolean (internal vs external edition), so we widen it here
 * rather than block on a `gen:types` refresh.
 */
interface EditionResponse {
  edition: string;
  is_internal: boolean;
}

export const useServiceStore = defineStore("service", () => {
  const health = ref<HealthResponse | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const rebooting = ref(false);
  /**
   * Whether this is the INTERNAL edition (cloud provider "qgenie"
   * pre-configured, so a missing API key is set in-place via the dialog).
   * `null` = not yet known: callers must treat unknown CONSERVATIVELY and
   * only take the in-place-dialog path when this is strictly `true`
   * (`isInternal === true`); any other value routes to Settings so we never
   * open a key dialog for a provider that does not exist on the external
   * edition.
   */
  const isInternal = ref<boolean | null>(null);

  async function fetchHealth(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      health.value = await apiJson<HealthResponse>("GET", "/api/system/health");
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  /**
   * Fetch the running edition once (internal vs external). Non-fatal: on
   * failure `isInternal` stays `null` (= unknown), which callers treat
   * conservatively (route to Settings rather than open the in-place dialog).
   */
  async function fetchEdition(): Promise<void> {
    try {
      const resp = await apiJson<EditionResponse>("GET", "/api/system/edition");
      isInternal.value = resp.is_internal;
    } catch {
      // Leave `isInternal` as-is (null when never fetched). The edition flag
      // is advisory for a guided flow, never blocking.
    }
  }

  async function reboot(): Promise<RebootResponse | null> {
    rebooting.value = true;
    error.value = null;
    try {
      const res = await apiJson<RebootResponse>("POST", "/api/system/reboot");
      return res;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
      return null;
    } finally {
      rebooting.value = false;
    }
  }

  return {
    health,
    loading,
    error,
    rebooting,
    isInternal,
    fetchHealth,
    fetchEdition,
    reboot,
  };
});
