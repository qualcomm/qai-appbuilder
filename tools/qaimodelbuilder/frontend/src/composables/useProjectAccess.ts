// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useProjectAccess` — project directory access control composable.
 *
 * V1 parity (`composables/useProjectAccess.js`): owns the server-side
 * `project_access` status (`enabled` / `path` / `skip_dirs`) and exposes
 * `fetchStatus` / `updateStatus`. The panel keeps its own editable draft
 * and only calls `updateStatus` on explicit save (matching V1's
 * draft + `hasUnsavedChanges` flow).
 *
 * V2 wiring notes:
 *   - Endpoint is `/api/security/project_access` (underscore). `GET` reads,
 *     `PUT` performs a partial update (V1 used POST; backend route is the
 *     already-locked PUT, behaviourally equivalent to V1's "save all").
 *   - The backend response returns `enabled` + `path` + `skip_dirs`.
 */
import { reactive, ref } from "vue";

import { apiJson } from "@/api";

/**
 * Default skip directories — 对齐 V1 实测默认值（18 项）。
 *
 * V1 后端 `backend/tools/_glob.py:_DEFAULT_SKIP_DIR_NAMES` 提供完整集合，
 * V1 前端 chips UI 实测渲染 18 项（不含 `.svelte-kit`）。这里作为前端
 * fallback：用户首次加载且后端 `skip_dirs` 为空时使用，让用户感知与 V1
 * 一致（"项目目录访问"面板初始就显示这 18 个 chips）。
 *
 * 顺序按 V1 chips 实测顺序：venv 类 → node 类 → Python cache → build 产物
 * → VCS → IDE。
 */
export const DEFAULT_SKIP_DIRS: readonly string[] = [
  // Python virtual environments
  "venv",
  ".venv",
  "env",
  ".env",
  // Node / JS
  "node_modules",
  ".next",
  ".nuxt",
  // Python cache
  "__pycache__",
  ".mypy_cache",
  ".pytest_cache",
  ".ruff_cache",
  // Build artefacts
  "build",
  "dist",
  // Version control
  ".git",
  ".hg",
  ".svn",
  // IDE / editor
  ".idea",
  ".vscode",
];

/** Server-side project access status. */
export interface ProjectAccessStatus {
  enabled: boolean;
  path: string;
  skip_dirs: string[];
}

/** Partial update payload sent to `PUT /api/security/project_access`. */
export interface ProjectAccessUpdate {
  enabled?: boolean;
  path?: string;
  skip_dirs?: string[];
}

/**
 * Backend response shape (`enabled` + `path` + `skip_dirs`).
 */
interface ProjectAccessResponse {
  enabled?: boolean;
  path?: string;
  skip_dirs?: string[];
}

const ENDPOINT = "/api/security/project_access";

export function useProjectAccess() {
  const status = reactive<ProjectAccessStatus>({
    enabled: false,
    path: "",
    skip_dirs: [],
  });

  const loading = ref(false);
  const saving = ref(false);
  const lastError = ref<string | null>(null);

  function applyResponse(data: ProjectAccessResponse): void {
    status.enabled = data.enabled ?? false;
    status.path = data.path ?? "";
    // Fallback to V1 18-item defaults when the backend omits the field
    // (first-load before any user save). Empty array is preserved as-is to
    // honour explicit user intent ("clear all skip dirs" must not re-fill).
    status.skip_dirs =
      data.skip_dirs === undefined ? [...DEFAULT_SKIP_DIRS] : data.skip_dirs;
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
