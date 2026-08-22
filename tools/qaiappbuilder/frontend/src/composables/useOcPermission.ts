// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useOcPermission — V1-parity OpenCode tool-permission composable.
 *
 * Extracted from `OpenCodeConfigPanel.vue` (cohesion split). Owns the
 * 4-state permission matrix (default / allow / ask / deny), the
 * per-state summary lists, and the advanced raw-JSON editor that is the
 * source of truth on save (V1 parity: matrix is the quick editor, the
 * textarea overrides / complements it).
 *
 * The composable reads/writes the host's reactive `cfg.permission` and
 * keeps its own `permissionText` / `permissionParseError`. No watch, no
 * lifecycle hooks: pure ref + computed + pure helpers. The host calls
 * `syncPermissionText()` after `loadConfig()` and validates via
 * `permissionFromText()` on save.
 */
import { computed, ref, type ComputedRef, type Ref } from "vue";
import { useI18n } from "vue-i18n";

/** Host config permission map: tool → state string OR per-pattern nested map. */
export type OcPermissionMap = Record<string, string | Record<string, string>>;

/** Minimal slice of the host's `OcConfig` this composable touches. */
export interface OcPermissionConfigShape {
  permission: OcPermissionMap;
}

export interface ToolState {
  id: string;
  label: string;
  icon: string;
  color: string;
}

export interface UseOcPermissionReturn {
  OC_TOOLS: { id: string }[];
  OC_STATES: ToolState[];
  ocToolStateOf: (toolId: string) => string;
  onOcToolCycle: (toolId: string, next: string) => void;
  ocAllowList: ComputedRef<string[]>;
  ocAskList: ComputedRef<string[]>;
  ocDenyList: ComputedRef<string[]>;
  permissionText: Ref<string>;
  permissionParseError: Ref<string>;
  permissionToText: (perm: OcPermissionMap) => string;
  permissionFromText: (text: string) => OcPermissionMap | null;
  syncPermissionText: () => void;
}

const OC_TOOLS = [
  "bash", "edit", "read", "grep", "glob", "list",
  "webfetch", "websearch", "task", "todoread", "todowrite",
  "codesearch", "lsp",
].map((id) => ({ id }));

export function useOcPermission<T extends OcPermissionConfigShape>(opts: {
  /** Reactive config object — read/written for tool permissions. */
  cfg: T;
}): UseOcPermissionReturn {
  const { t } = useI18n();
  const { cfg } = opts;

  const OC_STATES: ToolState[] = [
    { id: "default", label: t("aiCoding.config.toolStateDefault", "default"), icon: "○", color: "var(--text-muted)" },
    { id: "allow", label: t("aiCoding.config.ocAllow", "allow"), icon: "✓", color: "#4ade80" },
    { id: "ask", label: t("aiCoding.config.ocAsk", "ask"), icon: "?", color: "#fbbf24" },
    { id: "deny", label: t("aiCoding.config.ocDeny", "deny"), icon: "✕", color: "#f87171" },
  ];

  const permissionText = ref("");
  const permissionParseError = ref("");

  function permissionToText(perm: OcPermissionMap): string {
    if (!perm || Object.keys(perm).length === 0) return "";
    try {
      return JSON.stringify(perm, null, 2);
    } catch {
      return "";
    }
  }

  function permissionFromText(text: string): OcPermissionMap | null {
    const trimmed = text.trim();
    if (!trimmed) return {};
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        return null;
      }
      return parsed as OcPermissionMap;
    } catch {
      return null;
    }
  }

  function ocToolStateOf(toolId: string): string {
    const v = cfg.permission[toolId];
    if (!v) return "default";
    if (typeof v === "string") return v;
    // Nested object (e.g. {"git *":"allow"}) — surfaced via the advanced
    // JSON editor; the 4-state matrix shows it as "custom" (V1 parity).
    return "custom";
  }

  function onOcToolCycle(toolId: string, next: string): void {
    const map = { ...cfg.permission };
    if (next === "default") delete map[toolId];
    else map[toolId] = next;
    cfg.permission = map;
    // Keep the advanced JSON editor in sync (V1 parity).
    permissionText.value = permissionToText(cfg.permission);
  }

  // ─── Tool permission summary (V1 parity OpenCodeConfigPanel.js:731-745) ─────
  // Lists the tools currently in each simple state (string-valued entries).
  function ocToolsInState(state: string): string[] {
    return Object.entries(cfg.permission)
      .filter(([, v]) => v === state)
      .map(([k]) => k);
  }
  const ocAllowList = computed(() => ocToolsInState("allow"));
  const ocAskList = computed(() => ocToolsInState("ask"));
  const ocDenyList = computed(() => ocToolsInState("deny"));

  /** Refresh the advanced JSON editor text from the current config. */
  function syncPermissionText(): void {
    permissionText.value = permissionToText(cfg.permission);
  }

  return {
    OC_TOOLS,
    OC_STATES,
    ocToolStateOf,
    onOcToolCycle,
    ocAllowList,
    ocAskList,
    ocDenyList,
    permissionText,
    permissionParseError,
    permissionToText,
    permissionFromText,
    syncPermissionText,
  };
}
