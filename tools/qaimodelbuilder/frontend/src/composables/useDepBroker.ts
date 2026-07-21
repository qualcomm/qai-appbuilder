// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useDepBroker` — dependency-install approval queue.
 *
 * Surfaces the dep_broker enable switch plus the pending install-request
 * queue with approve / reject actions. The switch is part of the unified
 * runtime-config surface (2026-06 security-settings unification, 2026-06-13
 * follow-up): the legacy `GET/PUT /api/settings/dep_broker` route was deleted
 * along with the other "dead settings" KV sections, and `dependency_approval_enabled`
 * now rides `/api/security/runtime-config`. The pending / approve / reject
 * routes under `/api/security/dependency_approval/*` are unchanged.
 *
 * Maps 1:1 onto the real backend routes (verified against
 * `interfaces/http/routes/security/_runtime_config.py` +
 * `interfaces/http/routes/brokers.py`):
 *
 *   GET  /api/security/runtime-config             → { ..., dependency_approval_enabled }
 *   PUT  /api/security/runtime-config             → partial; { ..., dependency_approval_enabled?: bool }
 *   GET  /api/security/dependency_approval/pending → { pending: [...] }
 *   POST /api/security/dependency_approval/approve → { success, decision } (body { id })
 *   POST /api/security/dependency_approval/reject  → { success, decision } (body { id })
 */
import { ref, type Ref } from "vue";

import { apiJson } from "@/api";

// ─── Types (mirror real backend wire shapes) ──────────────────────────────────

export interface DepBrokerSettings {
  enabled: boolean;
}

export interface DepBrokerPendingRequest {
  id: string;
  command_args: string[];
  requester: string;
  created_at: string;
  status: string;
  // Tail-appended V1-parity fields (PendingRequest.command / .denied_args).
  command?: string;
  denied_args?: string[];
}

interface PendingListResponse {
  pending: DepBrokerPendingRequest[];
}

// Minimal slice of the runtime-config response we need here.
interface _RuntimeConfigSlice {
  dependency_approval_enabled?: boolean;
}

// ─── Composable ──────────────────────────────────────────────────────────────

export function useDepBroker() {
  const settings: Ref<DepBrokerSettings> = ref({
    enabled: false,
  });
  const pending: Ref<DepBrokerPendingRequest[]> = ref([]);
  const loading: Ref<boolean> = ref(false);
  const error: Ref<string | null> = ref(null);

  async function fetchSettings(): Promise<void> {
    try {
      const res = await apiJson<_RuntimeConfigSlice>(
        "GET",
        "/api/security/runtime-config",
      );
      settings.value = {
        enabled: Boolean(res.dependency_approval_enabled),
      };
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  async function setEnabled(enabled: boolean): Promise<void> {
    error.value = null;
    try {
      // dependency_approval_enabled is hot-applied (no needs_reboot), so the
      // partial PUT takes effect for the next intercepted exec immediately.
      await apiJson("PUT", "/api/security/runtime-config", {
        dependency_approval_enabled: enabled,
      });
      settings.value = { ...settings.value, enabled };
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
      // re-sync from server on failure so the toggle reflects reality
      await fetchSettings();
    }
  }

  async function fetchPending(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const res = await apiJson<PendingListResponse>("GET", "/api/security/dependency_approval/pending");
      pending.value = Array.isArray(res.pending) ? res.pending : [];
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  async function approve(id: string): Promise<void> {
    error.value = null;
    try {
      await apiJson("POST", "/api/security/dependency_approval/approve", { id });
      pending.value = pending.value.filter((r) => r.id !== id);
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  async function reject(id: string): Promise<void> {
    error.value = null;
    try {
      await apiJson("POST", "/api/security/dependency_approval/reject", { id });
      pending.value = pending.value.filter((r) => r.id !== id);
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  return {
    settings,
    pending,
    loading,
    error,
    fetchSettings,
    setEnabled,
    fetchPending,
    approve,
    reject,
  };
}
