// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useDangerousCommands` — custom dangerous-command patterns composable (P-10).
 *
 * Maps 1:1 onto the backend union-only override surface (verified against
 * `interfaces/http/routes/security/_dangerous_commands.py`):
 *
 *   GET /api/security/dangerous-command-patterns
 *     → { builtin: string[], extra: string[] }
 *   PUT /api/security/dangerous-command-patterns
 *     body { extra: string[] }
 *     → { builtin, extra, needs_reboot, invalid }
 *
 * The security domain owns an IMMUTABLE built-in floor (9 destructive-command
 * regexes like `rm -rf` / `format C:` / fork-bomb). Operators may only ADD
 * `extra` patterns on top — there is no field, on the backend or here, that can
 * delete a floor entry (red line §9.2.4). `builtin` is exposed read-only for
 * display; only `extra` is editable.
 *
 * Reboot: the extra patterns are baked into the FileBroker guard closure at
 * build time, so a save does NOT hot-apply — the response always carries
 * `needs_reboot=true` (same nature as `file_broker_enabled`). Any submitted
 * regex that fails to compile is dropped by the backend and echoed back in
 * `invalid` so the UI can warn the operator.
 */
import { ref, type Ref } from "vue";

import { apiJson } from "@/api";

interface DangerousCommandPatternsResponse {
  builtin?: string[];
  extra?: string[];
}

interface DangerousCommandPatternsUpdateResponse
  extends DangerousCommandPatternsResponse {
  needs_reboot?: boolean;
  invalid?: string[];
}

export function useDangerousCommands() {
  // Read-only immutable floor (regex source strings) for display.
  const builtin: Ref<string[]> = ref([]);
  // Operator-editable union-only extra patterns.
  const extra: Ref<string[]> = ref([]);
  const loading = ref(false);
  const saving = ref(false);
  const error: Ref<string | null> = ref(null);

  async function fetchPatterns(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const res = await apiJson<DangerousCommandPatternsResponse>(
        "GET",
        "/api/security/dangerous-command-patterns",
      );
      builtin.value = Array.isArray(res?.builtin) ? res.builtin.map(String) : [];
      extra.value = Array.isArray(res?.extra) ? res.extra.map(String) : [];
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  /**
   * Persist the current `extra` list. Returns whether a restart is required
   * (`needsReboot`, always true on success — the patterns are baked at build)
   * plus any `invalid` regex strings the backend dropped.
   */
  async function save(): Promise<{ needsReboot: boolean; invalid: string[] }> {
    saving.value = true;
    error.value = null;
    try {
      const res = await apiJson<DangerousCommandPatternsUpdateResponse>(
        "PUT",
        "/api/security/dangerous-command-patterns",
        { extra: [...extra.value] },
      );
      // Reflect the server-accepted (deduped / validated) view.
      if (Array.isArray(res?.extra)) extra.value = res.extra.map(String);
      if (Array.isArray(res?.builtin)) builtin.value = res.builtin.map(String);
      return {
        needsReboot: Boolean(res?.needs_reboot),
        invalid: Array.isArray(res?.invalid) ? res.invalid.map(String) : [],
      };
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
      await fetchPatterns();
      return { needsReboot: false, invalid: [] };
    } finally {
      saving.value = false;
    }
  }

  function addExtra(pattern: string): void {
    const p = pattern.trim();
    if (p && !extra.value.includes(p)) {
      extra.value = [...extra.value, p];
    }
  }

  function removeExtra(pattern: string): void {
    extra.value = extra.value.filter((p) => p !== pattern);
  }

  function updateExtra(index: number, pattern: string): void {
    if (index < 0 || index >= extra.value.length) return;
    const next = [...extra.value];
    next[index] = pattern;
    extra.value = next;
  }

  return {
    // state
    builtin,
    extra,
    loading,
    saving,
    error,
    // actions
    fetchPatterns,
    save,
    addExtra,
    removeExtra,
    updateExtra,
  };
}
