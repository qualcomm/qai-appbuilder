// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useSecurityOverview — Overview-tab presentation logic (V1 parity).
 *
 * Ports the verified V1 `SecurityConfigPanel.js` Overview behaviour
 * (permissionSummary computed at 661-724, dry-run runCheck at 826-842,
 * health polling at 610-631) into a single composable so `SecurityView`
 * stays a thin template host (AGENTS.md §重构质量铁律 need A: cohesion —
 * the巨型 component does not平铺 all this state inline).
 *
 * Behaviour == V1; data mapping adapts to the V2 Clean-Cutover backend:
 *
 *   - V1 derived read/write/run from the flat `read_allow`/`write_allow`/
 *     `exec_allow_cwd` arrays + per-skill `raw_read`/`raw_write`/
 *     `trusted_binaries`. V2's Policy is a rules-based allow/deny list
 *     (no per-op arrays), so the BASE row shows the policy's `allow`
 *     rule patterns, and the SKILL groups come from
 *     `GET /api/security/skill-discovery` (`read_paths` / `write_paths` /
 *     `exec_paths` / `trusted_binaries` — exact V1 field parity). All
 *     discovered skills are active (the endpoint returns `list_active`).
 *   - The dry-run tester calls `POST /api/security/permission/check`
 *     (the audit-backed CheckPermissionUseCase), the live V2 equivalent
 *     of V1's `/api/security/check`. (The audit-free
 *     `/api/security/sandbox/test` route was removed in the 2026-07-01
 *     sandbox cleanup; see `runDryRun` below.)
 */
import {
  computed,
  ref,
  onActivated,
  onBeforeUnmount,
  onDeactivated,
  onMounted,
} from "vue";
import { apiJson } from "@/api";
import {
  useSecurityStore,
  type DiscoveredSkill,
} from "@/stores/security";

/** V1 health poll cadence — SecurityConfigPanel.js:615. */
const HEALTH_POLL_MS = 30000;

export interface SkillPermGroup {
  skillName: string;
  active: boolean;
  paths: string[];
}

export interface DryRunMatchedRule {
  type: string;
  source: string;
  rule_id?: string;
}

export interface DryRunResult {
  decision: "allow" | "ask" | "deny" | "error";
  reason: string;
  // V1 parity (SecurityConfigPanel.js:1159-1162): the matched rule's
  // op (type) + scope (source) + a human explanation. permission/check
  // returns only matched ids, so these are synthesised in `runDryRun`.
  matchedRule?: DryRunMatchedRule | null;
  explanation?: string | null;
}

interface CheckPermissionResponse {
  decision: "allow" | "deny";
  matched_rule_id: string | null;
  matched_grant_id: string | null;
  audit_id: string;
}

export function useSecurityOverview(options?: { autoPollHealth?: boolean }) {
  const autoPollHealth = options?.autoPollHealth ?? true;
  const store = useSecurityStore();

  // ── Accordion expand state (V1 overviewExpanded) ────────────────────
  const expanded = ref<{ read: boolean; write: boolean; run: boolean }>({
    read: false,
    write: false,
    run: false,
  });
  function toggle(section: "read" | "write" | "run"): void {
    expanded.value[section] = !expanded.value[section];
  }

  // ── Base policy allowed patterns (rules-based model) ────────────────
  const baseAllowedPaths = computed<string[]>(() =>
    (store.policy?.rules ?? [])
      .filter((r) => r.action === "allow")
      .map((r) => r.pattern),
  );

  function skillGroups(
    field: "read_paths" | "write_paths" | "exec_paths",
  ): SkillPermGroup[] {
    return (store.discoveredSkills as DiscoveredSkill[])
      .map((s) => ({
        skillName: s.skill_name || s.capability_name,
        // V1 parity (SecurityConfigPanel.js:661-724): read s.active to detect
        // inactive skills so hasInactiveRead/hasInactiveWrite can be derived.
        active: s.active ?? true,
        paths: s[field] ?? [],
      }))
      .filter((g) => g.paths.length > 0);
  }

  const trustedBySkill = computed<SkillPermGroup[]>(() =>
    (store.discoveredSkills as DiscoveredSkill[])
      .map((s) => ({
        skillName: s.skill_name || s.capability_name,
        active: s.active ?? true,
        paths: s.trusted_binaries ?? [],
      }))
      .filter((g) => g.paths.length > 0),
  );

  /**
   * V1 workspaceDirs parity (SecurityConfigPanel.js:701-708):
   * exec_allow_cwd dirs that are NOT overlapping with write_allow dirs.
   * These are "read-only workspace dirs" where binaries can run silently.
   * In V2: exec-allow rules minus write-allow rules (path overlap filter).
   */
  const workspaceDirs = computed<string[]>(() => {
    const rules = store.policy?.rules ?? [];
    const execDirs = rules
      .filter((r) => r.op === "exec" && r.action === "allow")
      .map((r) => r.pattern);
    const writeDirs = rules
      .filter((r) => r.op === "write" && r.action === "allow")
      .map((r) => r.pattern.toLowerCase().replace(/\\/g, "/"));
    return execDirs.filter((dir) => {
      const dL = dir.toLowerCase().replace(/\\/g, "/");
      const dSlash = dL.endsWith("/") ? dL : dL + "/";
      return !writeDirs.some((w) => {
        const wSlash = w.endsWith("/") ? w : w + "/";
        return dSlash.startsWith(wSlash) || wSlash.startsWith(dSlash);
      });
    });
  });

  /** V1 permissionSummary parity (read / write / run breakdown). */
  const permissionSummary = computed(() => {
    const base = baseAllowedPaths.value;
    const readGroups = skillGroups("read_paths");
    const writeGroups = skillGroups("write_paths");
    const runGroups = trustedBySkill.value;
    const sum = (groups: SkillPermGroup[]): number =>
      groups.reduce((n, g) => n + g.paths.length, 0);
    return {
      baseAllowed: base,
      read: readGroups,
      write: writeGroups,
      run: runGroups,
      totalRead: base.length + sum(readGroups),
      totalWrite: base.length + sum(writeGroups),
      totalRun: sum(runGroups),
      dynamicAuth: store.policy?.dynamic_authorization ?? true,
      // V1 parity (SecurityConfigPanel.js:717-718): show "含未激活" badge
      // when any skill contributing to read/write paths is inactive.
      hasInactiveRead: readGroups.some((g) => !g.active),
      hasInactiveWrite: writeGroups.some((g) => !g.active),
      // V1 parity (SecurityConfigPanel.js:701-708, 1097-1101): workspace dirs
      // shown in the expanded run section.
      workspaceDirs: workspaceDirs.value,
    };
  });

  // ── IM channel dialog toggles (no_ui_channels — V1 171-178) ─────────
  const wechatDialogEnabled = computed<boolean>(
    () => !(store.policy?.no_ui_channels ?? []).includes("wechat"),
  );
  const feishuDialogEnabled = computed<boolean>(
    () => !(store.policy?.no_ui_channels ?? []).includes("feishu"),
  );

  /** Toggle a channel's WebUI dialog gate (V1 toggleImChannelDialog). */
  async function toggleImChannelDialog(
    channel: "wechat" | "feishu",
    enableDialog: boolean,
  ): Promise<void> {
    const current = [...(store.policy?.no_ui_channels ?? ["wechat", "feishu"])];
    let next: string[];
    if (enableDialog) {
      next = current.filter((c) => c !== channel);
    } else {
      if (current.includes(channel)) return;
      next = [...current, channel];
    }
    await store.updatePolicyToggles({ no_ui_channels: next });
  }

  // ── Dry-run tester (V1 runCheck → V2 audited /permission/check) ─────
  const dryRunOp = ref<"execute" | "read" | "write">("execute");
  const dryRunPath = ref("");
  const dryRunResult = ref<DryRunResult | null>(null);
  const dryRunLoading = ref(false);

  async function runDryRun(): Promise<void> {
    const target = dryRunPath.value.trim();
    if (!target) return;
    dryRunLoading.value = true;
    dryRunResult.value = null;
    const op = dryRunOp.value;
    try {
      // The old audit-free POST /api/security/sandbox/test route was removed
      // in the 2026-07-01 sandbox cleanup. The live equivalent is the
      // audit-backed CheckPermissionUseCase at POST /api/security/permission/
      // check, which takes a Subject + Resource + AceMask. The Overview
      // tester has no bound coding session, so we synthesise the same
      // {kind:"user"} subject the channel bridges use with a stable "webui"
      // identifier.
      const res = await apiJson<CheckPermissionResponse>(
        "POST",
        "/api/security/permission/check",
        {
          subject: { kind: "user", identifier: "webui" },
          resource: {
            kind: op === "execute" ? "exec" : "path",
            identifier: target,
          },
          requested_mask: {
            read: op === "read",
            write: op === "write",
            execute: op === "execute",
            delete: false,
          },
        },
      );
      // permission/check returns only allow|deny + matched ids (no reason
      // string, no "ask"), so synthesise the reason/matchedRule the result
      // renderer expects from the matched rule/grant id.
      const matchedId = res.matched_rule_id ?? res.matched_grant_id;
      dryRunResult.value = {
        decision: res.decision,
        reason: matchedId
          ? `${res.decision} · ${matchedId}`
          : `${res.decision} · default-deny`,
        matchedRule: matchedId
          ? {
              type: op,
              source: res.matched_rule_id ? "rule" : "grant",
              rule_id: matchedId,
            }
          : null,
        explanation: null,
      };
    } catch (e) {
      dryRunResult.value = {
        decision: "error",
        reason: e instanceof Error ? e.message : "request_failed",
      };
    } finally {
      dryRunLoading.value = false;
    }
  }

  // ── Health polling (V1 30s interval; persists while view mounted) ───
  let healthTimer: number | null = null;
  function startHealthPolling(): void {
    if (healthTimer !== null) return; // already polling — keep the existing timer
    void store.fetchHealth();
    healthTimer = window.setInterval(() => {
      void store.fetchHealth();
    }, HEALTH_POLL_MS);
  }
  function stopHealthPolling(): void {
    if (healthTimer !== null) {
      window.clearInterval(healthTimer);
      healthTimer = null;
    }
  }

  if (autoPollHealth) {
    // Lifecycle is KeepAlive-aware: SecurityView is cached by AppMain.vue's
    // <KeepAlive>, so we must pause the 30s health poller on `onDeactivated`
    // (hide) and resume on `onActivated` (show). Cleaning up only in
    // `onBeforeUnmount` would leak the timer indefinitely after the user
    // navigates away from /security. `startHealthPolling` is idempotent (it
    // calls `stopHealthPolling()` at the top), so onActivated firing right
    // after the first onMounted is harmless.
    onMounted(startHealthPolling);
    onActivated(startHealthPolling);
    onDeactivated(stopHealthPolling);
    onBeforeUnmount(stopHealthPolling);
  }

  return {
    expanded,
    toggle,
    permissionSummary,
    workspaceDirs,
    wechatDialogEnabled,
    feishuDialogEnabled,
    toggleImChannelDialog,
    dryRunOp,
    dryRunPath,
    dryRunResult,
    dryRunLoading,
    runDryRun,
    startHealthPolling,
    stopHealthPolling,
  };
}
