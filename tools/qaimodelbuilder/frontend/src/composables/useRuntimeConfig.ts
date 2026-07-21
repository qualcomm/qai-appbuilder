// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useRuntimeConfig` — unified security + tools runtime-config surface.
 *
 * Maps 1:1 onto the real backend routes (verified against
 * `interfaces/http/routes/security/_runtime_config.py`):
 *
 *   GET /api/security/runtime-config
 *     → { file_broker_enabled, ssl_verify, project_skip_dirs, global_proxy,
 *         file_guard_enabled, allow_exec_tool, sandbox_enabled }
 *   PUT /api/security/runtime-config (partial; every field optional)
 *     → { ...effective view, needs_reboot, persisted }
 *
 * This is the single authoritative surface for the typed security/tools
 * switches the WebUI exposes (2026-06 security-settings unification). Every
 * field drives real backend behaviour — it replaces the six dead
 * `/api/settings/*` KV sections that had no consumer.
 *
 * Reboot (decision 3B): changing a build-time switch (`file_guard_enabled` /
 * `allow_exec_tool` / `sandbox_enabled` / `file_broker_enabled`) returns
 * `needs_reboot=true`; callers show the custom reboot-confirm dialog and, on
 * confirm, drive `useReboot`. The hot-applicable tools switches (`ssl_verify`
 * / `project_skip_dirs` / `global_proxy`) take effect immediately.
 */
import { ref, type Ref } from "vue";

import { apiJson } from "@/api";

export interface RuntimeConfig {
  // Layer 1 — tool safety (ToolsSettings)
  file_broker_enabled: boolean;
  file_broker_max_entries: number;
  ssl_verify: boolean;
  project_skip_dirs: string[];
  global_proxy: string | null;
  // Layer 2 — policy guard (SecuritySettings)
  file_guard_enabled: boolean;
  // OS-level FileGuard hook for subprocesses (hot-applied, no reboot). Driven
  // together with `file_guard_enabled` by the single unified FileGuard switch.
  native_file_guard_enabled: boolean;
  allow_exec_tool: boolean;
  // Layer 3 — OS isolation (SecuritySettings)
  sandbox_enabled: boolean;
  // dependency_approval — controlled dependency-install approval (hot-applied)
  dependency_approval_enabled: boolean;
  // Tool output limits — caps on the result volume each tool hands back to the
  // model (build-time → reboot, same nature as file_broker_max_entries).
  read_max_lines: number;
  read_max_bytes: number;
  read_max_line_length: number;
  glob_max_results: number;
  grep_max_matches: number;
  grep_max_line_length: number;
  grep_max_output_bytes: number;
}

interface RuntimeConfigUpdateResponse extends RuntimeConfig {
  needs_reboot: boolean;
  persisted: string[];
}

const DEFAULTS: RuntimeConfig = {
  file_broker_enabled: true,
  file_broker_max_entries: 10000,
  ssl_verify: true,
  project_skip_dirs: [],
  global_proxy: null,
  file_guard_enabled: false,
  native_file_guard_enabled: false,
  allow_exec_tool: true,
  sandbox_enabled: false,
  dependency_approval_enabled: false,
  read_max_lines: 2000,
  read_max_bytes: 102400,
  read_max_line_length: 2000,
  glob_max_results: 100,
  grep_max_matches: 100,
  grep_max_line_length: 2000,
  grep_max_output_bytes: 51200,
};

export function useRuntimeConfig() {
  const config: Ref<RuntimeConfig> = ref({ ...DEFAULTS });
  const loading = ref(false);
  const error: Ref<string | null> = ref(null);

  function _coerce(res: Partial<RuntimeConfig>): RuntimeConfig {
    return {
      file_broker_enabled: Boolean(res.file_broker_enabled),
      file_broker_max_entries: Number(res.file_broker_max_entries ?? 10000),
      ssl_verify: Boolean(res.ssl_verify),
      project_skip_dirs: Array.isArray(res.project_skip_dirs)
        ? res.project_skip_dirs.map(String)
        : [],
      global_proxy: res.global_proxy ?? null,
      file_guard_enabled: Boolean(res.file_guard_enabled),
      native_file_guard_enabled: Boolean(res.native_file_guard_enabled),
      allow_exec_tool: Boolean(res.allow_exec_tool),
      sandbox_enabled: Boolean(res.sandbox_enabled),
      dependency_approval_enabled: Boolean(res.dependency_approval_enabled),
      read_max_lines: Number(res.read_max_lines ?? 2000),
      read_max_bytes: Number(res.read_max_bytes ?? 102400),
      read_max_line_length: Number(res.read_max_line_length ?? 2000),
      glob_max_results: Number(res.glob_max_results ?? 100),
      grep_max_matches: Number(res.grep_max_matches ?? 100),
      grep_max_line_length: Number(res.grep_max_line_length ?? 2000),
      grep_max_output_bytes: Number(res.grep_max_output_bytes ?? 51200),
    };
  }

  async function fetchConfig(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const res = await apiJson<RuntimeConfig>("GET", "/api/security/runtime-config");
      config.value = _coerce(res);
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  /**
   * Persist a partial update. Returns whether a restart is required for the
   * change to take effect (`needs_reboot`); callers use it to decide whether
   * to prompt the reboot-confirm dialog.
   */
  async function save(patch: Partial<RuntimeConfig>): Promise<{ needsReboot: boolean }> {
    error.value = null;
    try {
      const res = await apiJson<RuntimeConfigUpdateResponse>(
        "PUT",
        "/api/security/runtime-config",
        patch,
      );
      config.value = _coerce(res);
      return { needsReboot: Boolean(res.needs_reboot) };
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
      await fetchConfig();
      return { needsReboot: false };
    }
  }

  return { config, loading, error, fetchConfig, save };
}
