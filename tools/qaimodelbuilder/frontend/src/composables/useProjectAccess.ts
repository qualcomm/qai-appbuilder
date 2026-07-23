// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useProjectAccess` — project directory access control composable.
 *
 * V1 parity (`composables/useProjectAccess.js`): owns the server-side
 * `project_access` status (`enabled` / `path`) and exposes
 * `fetchStatus` / `updateStatus`. The panel keeps its own editable draft
 * and only calls `updateStatus` on explicit save (matching V1's
 * draft + `hasUnsavedChanges` flow).
 *
 * V2 wiring notes:
 *   - Endpoint is `/api/security/project_access` (underscore). `GET` reads,
 *     `PUT` performs a partial update (V1 used POST; backend route is the
 *     already-locked PUT, behaviourally equivalent to V1's "save all").
 *   - The backend response returns `enabled` + `path`.
 */
import { reactive, ref } from "vue";

import { apiJson } from "@/api";

/** Server-side project access status. */
export interface ProjectAccessStatus {
  enabled: boolean;
  path: string;
}

/** Partial update payload sent to `PUT /api/security/project_access`. */
export interface ProjectAccessUpdate {
  enabled?: boolean;
  path?: string;
}

/**
 * Backend response shape (`enabled` + `path`).
 */
interface ProjectAccessResponse {
  enabled?: boolean;
  path?: string;
}

const ENDPOINT = "/api/security/project_access";

export function useProjectAccess() {
  const status = reactive<ProjectAccessStatus>({
    enabled: false,
    path: "",
  });

  const loading = ref(false);
  const saving = ref(false);
  const lastError = ref<string | null>(null);

  function applyResponse(data: ProjectAccessResponse): void {
    status.enabled = data.enabled ?? false;
    status.path = data.path ?? "";
  }

  async function fetchStatus(): Promise<void> {
    loading.value = true;
    lastError.value = null;
    try {
      const data = await apiJson<ProjectAccessResponse>("GET", ENDPOINT);
      applyResponse(data);
    } catch (e) {
      lastError.value = (e as Error).message || String(e);
    } finally {
      loading.value = false;
    }
  }

  async function updateStatus(updates: ProjectAccessUpdate): Promise<void> {
    saving.value = true;
    lastError.value = null;
    try {
      const data = await apiJson<ProjectAccessResponse, ProjectAccessUpdate>(
        "PUT",
        ENDPOINT,
        updates,
      );
      applyResponse(data);
    } catch (e) {
      lastError.value = (e as Error).message || String(e);
      throw e;
    } finally {
      saving.value = false;
    }
  }

  return {
    status,
    loading,
    saving,
    lastError,
    fetchStatus,
    updateStatus,
  };
}
