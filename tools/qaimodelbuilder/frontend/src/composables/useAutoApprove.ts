// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useAutoApprove` — V1-aligned auto-approve settings composable.
 *
 * Mirrors V1 `frontend/js/composables/useAutoApprove.js` (202 lines)
 * with TS + V2 conventions (Pinia `useToastStore`, `apiJson`).
 *
 * Manages five sections from V1 AutoApprovePanel:
 *   1. Tool-level auto-approve toggles  (read/write/exec/glob/grep)
 *   2. Command whitelist                (enabled + prefixes[])
 *   3. Command blacklist                (enabled defaults true + prefixes[])
 *   4. Read-allow path patterns         (enabled + patterns[])
 *   5. Write-allow path patterns        (enabled + patterns[])
 *
 * Backend endpoints (V1 wire schema, newly added in V2 alongside the
 * already-locked `/auto_approve/config` (different path, do NOT confuse)):
 *   GET    /api/security/auto_approve   → AutoApproveFullResponse
 *   PUT    /api/security/auto_approve   → AutoApproveFullResponse
 *   GET    /api/security/path_patterns  → PathPatternsResponse  (tail-appended fields)
 *   PUT    /api/security/path_patterns  → PathPatternsResponse
 *
 * `saveAll()` issues both PUTs in parallel (V1 `Promise.all` semantics).
 */
import { reactive, ref, type Ref } from "vue";
import { useI18n } from "vue-i18n";

import { apiJson } from "@/api";
import { useToastStore } from "@/stores/toast";

// ─── Types ───────────────────────────────────────────────────────────────────

export interface AutoApproveToggles {
  read: boolean;
  write: boolean;
  exec: boolean;
  glob: boolean;
  grep: boolean;
}

export interface CommandListConfig {
  enabled: boolean;
  prefixes: string[];
}

export interface PathPatternConfig {
  enabled: boolean;
  patterns: string[];
}

interface AutoApproveFullResponse {
  auto_approve?: Partial<AutoApproveToggles>;
  command_whitelist?: { enabled?: boolean; prefixes?: string[] };
  command_blacklist?: { enabled?: boolean; prefixes?: string[] };
}

interface PathPatternsFullResponse {
  // V2 already-locked deny/allow are returned too but we ignore them here;
  // this composable only manages the V1 read_allow / write_allow segment.
  read_allow_patterns?: { enabled?: boolean; patterns?: string[] };
  write_allow_patterns?: { enabled?: boolean; patterns?: string[] };
}

// ─── Composable ──────────────────────────────────────────────────────────────

/**
 * Default command blacklist prefixes — 对齐 V1 实测默认值（4 条预设）。
 *
 * V1 真值源：`config/access_policy.default.json` 与
 * `data/sandboxes/default/sandbox_fileguard_policy.json` 的
 * `command_blacklist.prefixes`。这是 V1 出厂安全基线（curl|sh / wget|sh /
 * powershell -enc / cmd /c del），用户首次加载且后端未提供该字段时使用，
 * 让"自动审批"面板的命令黑名单与 V1 用户感知一致（默认就有这 4 条防护）。
 *
 * 用户明确清空（保存空数组）时不会被覆盖（仅在响应字段缺失时 fallback）。
 */
export const DEFAULT_COMMAND_BLACKLIST_PREFIXES: readonly string[] = [
  "curl|sh",
  "wget|sh",
  "powershell -enc",
  "cmd /c del",
];

export function useAutoApprove() {
  const toast = useToastStore();
  const { t } = useI18n();

  const loading: Ref<boolean> = ref(false);
  const saving: Ref<boolean> = ref(false);

  // Tool-level toggles. V1 default: all false.
  const autoApprove = reactive<AutoApproveToggles>({
    read: false,
    write: false,
    exec: false,
    glob: false,
    grep: false,
  });

  // Command whitelist: V1 default disabled.
  const commandWhitelist = reactive<CommandListConfig>({
    enabled: false,
    prefixes: [],
  });

  // Command blacklist: V1 default ENABLED (safety default).
  const commandBlacklist = reactive<CommandListConfig>({
    enabled: true,
    prefixes: [...DEFAULT_COMMAND_BLACKLIST_PREFIXES],
  });

  // Path patterns.
  const readPatterns = reactive<PathPatternConfig>({
    enabled: false,
    patterns: [],
  });
  const writePatterns = reactive<PathPatternConfig>({
    enabled: false,
    patterns: [],
  });

  function pushError(prefix: string, e: unknown): void {
    const msg = e instanceof Error ? e.message : String(e);
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: `${prefix}: ${msg}`,
      timeoutMs: 5000,
    });
  }

  function pushSuccess(message: string): void {
    toast.push({
      id: crypto.randomUUID(),
      kind: "success",
      message,
      timeoutMs: 3000,
    });
  }

  // ── Load (V1 parity) ──
  async function loadSettings(): Promise<void> {
    loading.value = true;
    try {
      const [approveData, patternsData] = await Promise.all([
        apiJson<AutoApproveFullResponse>("GET", "/api/security/auto_approve"),
        apiJson<PathPatternsFullResponse>("GET", "/api/security/path_patterns"),
      ]);

      const aa = approveData?.auto_approve;
      if (aa) {
        autoApprove.read = !!aa.read;
        autoApprove.write = !!aa.write;
        autoApprove.exec = !!aa.exec;
        autoApprove.glob = !!aa.glob;
        autoApprove.grep = !!aa.grep;
      }
      const wl = approveData?.command_whitelist;
      if (wl) {
        commandWhitelist.enabled = !!wl.enabled;
        commandWhitelist.prefixes = wl.prefixes ?? [];
      }
      const bl = approveData?.command_blacklist;
      if (bl) {
        // V1: blacklist defaults to enabled — only false when explicitly false.
        commandBlacklist.enabled = bl.enabled !== false;
        // Fallback to V1 4-item presets when the backend omits `prefixes`
        // (first-load before any user save). Empty array is preserved
        // as-is to honour explicit user intent ("clear all blacklist").
        commandBlacklist.prefixes =
          bl.prefixes === undefined
            ? [...DEFAULT_COMMAND_BLACKLIST_PREFIXES]
            : bl.prefixes;
      }

      const rp = patternsData?.read_allow_patterns;
      if (rp) {
        readPatterns.enabled = !!rp.enabled;
        readPatterns.patterns = rp.patterns ?? [];
      }
      const wp = patternsData?.write_allow_patterns;
      if (wp) {
        writePatterns.enabled = !!wp.enabled;
        writePatterns.patterns = wp.patterns ?? [];
      }
    } catch (e) {
      pushError(t("security.autoApprove.loadFailed", "Failed to load auto-approve settings"), e);
    } finally {
      loading.value = false;
    }
  }

  // ── Save auto-approve (toggles + whitelist + blacklist) ──
  async function saveAutoApprove(): Promise<void> {
    await apiJson("PUT", "/api/security/auto_approve", {
      auto_approve: { ...autoApprove },
      command_whitelist: {
        enabled: commandWhitelist.enabled,
        prefixes: [...commandWhitelist.prefixes],
      },
      command_blacklist: {
        enabled: commandBlacklist.enabled,
        prefixes: [...commandBlacklist.prefixes],
      },
    });
  }

  // ── Save path patterns (read + write allow patterns) ──
  async function savePathPatterns(): Promise<void> {
    await apiJson("PUT", "/api/security/path_patterns", {
      read_allow_patterns: {
        enabled: readPatterns.enabled,
        patterns: [...readPatterns.patterns],
      },
      write_allow_patterns: {
        enabled: writePatterns.enabled,
        patterns: [...writePatterns.patterns],
      },
    });
  }

  // ── Save all (V1: Promise.all) ──
  async function saveAll(): Promise<void> {
    saving.value = true;
    const results = await Promise.allSettled([
      saveAutoApprove(),
      savePathPatterns(),
    ]);
    saving.value = false;

    const failures = results.filter(
      (r): r is PromiseRejectedResult => r.status === "rejected",
    );
    if (failures.length === 0) {
      pushSuccess(t("security.autoApprove.saved", "Auto-approve settings saved"));
    } else {
      for (const f of failures) {
        pushError(t("security.autoApprove.saveFailed", "Save failed"), f.reason);
      }
    }
  }

  // ── Reset to defaults (V1 parity, frontend-only) ──
  function resetDefaults(): void {
    autoApprove.read = false;
    autoApprove.write = false;
    autoApprove.exec = false;
    autoApprove.glob = false;
    autoApprove.grep = false;

    commandWhitelist.enabled = false;
    commandWhitelist.prefixes = [];

    commandBlacklist.enabled = true;
    commandBlacklist.prefixes = [...DEFAULT_COMMAND_BLACKLIST_PREFIXES];

    readPatterns.enabled = false;
    readPatterns.patterns = [];

    writePatterns.enabled = false;
    writePatterns.patterns = [];
  }

  return {
    // state
    loading,
    saving,
    autoApprove,
    commandWhitelist,
    commandBlacklist,
    readPatterns,
    writePatterns,
    // actions
    loadSettings,
    saveAutoApprove,
    savePathPatterns,
    saveAll,
    resetDefaults,
  };
}
