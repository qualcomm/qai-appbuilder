<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Security Overview panel — V1 full-parity (path C).
 *
 * Extracted verbatim from SecurityView's inline Overview tab so the view
 * stays a thin per-tab container (one <Panel> per tab, AGENTS.md need A:
 * cohesion). Behaviour is unchanged from the previous inline block:
 * permission summary accordion + dry-run tester + policy templates + dep
 * broker + FileGuard status card (master switch / run mode / dynamic auth /
 * smart approval) + IM channel authorization + stat tiles + actions.
 *
 * The presentation logic lives in `useSecurityOverview`.
 *
 * - `refreshAll()` is exposed (defineExpose) so the SecurityView header's
 *   "↺ refresh" button can drive it (V1 parity).
 * - "view full audit" emits `view-audit` so the parent can switch tab,
 *   replacing the previous inline `activeTab = 'audit'`.
 */
import { onActivated, onBeforeUnmount, onDeactivated, onMounted } from "vue";
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { useSecurityStore } from "@/stores/security";
import type { SecurityMode } from "@/stores/security";
import { useDepBroker } from "@/composables/useDepBroker";
import { useSecurityOverview } from "@/composables/security/useSecurityOverview";
import { useConfirm } from "@/composables/useConfirm";
import EmptyState from "@/components/common/EmptyState.vue";

const emit = defineEmits<{
  (e: "view-audit"): void;
}>();

const { t } = useI18n();
const store = useSecurityStore();
const depBroker = useDepBroker();
const overview = useSecurityOverview();
const { confirm } = useConfirm();

/**
 * Native sub-process hook display state (READ-ONLY; 🔴 State-Truth-First).
 * Derived from the polled `GET /api/security/health`:
 *   - "active"   — native_enabled && native_active (guard64.dll live)
 *   - "degraded" — native_enabled && !native_active (requested but DLL
 *                  failed to load/install: sub-process writes UNGUARDED)
 *   - "off"      — native hook not requested (native_enabled=false)
 */
const nativeHookState = computed<"active" | "degraded" | "off">(() => {
  const h = store.health;
  if (!h.native_enabled) return "off";
  return h.native_active ? "active" : "degraded";
});

/**
 * Security master switch (3c switch-tree §6.4) — the NEW 3-state master
 * control (enforcing | permissive | disabled), ORTHOGONAL to the locked
 * enforce/audit_only run-mode sub-switch (`setMode`). Bound to
 * `store.securityMode`, driven by `PUT /api/security/mode` via
 * `store.setSecurityMode`.
 */
const securityMode = computed<SecurityMode>(() => store.securityMode);

/**
 * When the master switch is `permissive` or `disabled`, the decision core
 * is forced to `audit_only` (see backend `effective_run_mode`), so the
 * enforce/audit_only run-mode sub-switch is meaningless — grey it out.
 * Reuses the P-11 native-disabled pattern (`:disabled` + reduced opacity /
 * not-allowed cursor + explanatory `title`).
 */
const runModeDisabled = computed<boolean>(
  () => store.securityMode !== "enforcing",
);

async function onSetSecurityMode(next: SecurityMode): Promise<void> {
  if (next === store.securityMode) return;
  if (next === "disabled") {
    const ok = await confirm({
      icon: "🛡️",
      title: t("security.masterMode.label"),
      message: t("security.masterMode.disabledConfirm"),
      confirmStyle: "danger",
      confirmText: t("common.confirm"),
      cancelText: t("common.cancel"),
    });
    if (!ok) return;
  }
  await store.setSecurityMode(next);
}

/**
 * Poll dep_broker pending queue every 5s while the panel is visible
 * (V1 SecurityConfigPanel parity). Lifecycle is KeepAlive-aware: SecurityView
 * is cached by `AppMain.vue`'s <KeepAlive>, so we must stop the poller on
 * `onDeactivated` (hide) and restart on `onActivated` (show). Cleaning up
 * only in `onBeforeUnmount` would leak the 5s timer forever once the user
 * navigates away from /security.
 */
let depBrokerTimer: number | null = null;

function startDepBrokerPoll(): void {
  if (depBrokerTimer !== null) return; // already polling
  depBrokerTimer = window.setInterval(() => {
    void depBroker.fetchPending();
  }, 5000);
}

function stopDepBrokerPoll(): void {
  if (depBrokerTimer !== null) {
    window.clearInterval(depBrokerTimer);
    depBrokerTimer = null;
  }
}

function loadOverview(): void {
  void store.fetchPending();
  void store.fetchPolicy();
  void store.fetchProcessGrants();
  // V1-parity tile: query session grants. The Overview does not currently
  // track an active coding-session id, so this initially populates the list
  // empty (count = 0), matching V1's "no active session" state. When a
  // coding-session id becomes available elsewhere it can call
  // `store.fetchSessionGrants(sid)` to refresh.
  void store.fetchSessionGrants();
  void store.fetchPolicyTemplates();
  void store.fetchSkillDiscovery();
  void depBroker.fetchSettings();
  void depBroker.fetchPending();
}

onMounted(() => {
  loadOverview();
  startDepBrokerPoll();
});

// KeepAlive: re-fetch + restart poller every time the panel becomes visible.
// Re-fetching on every RE-activation is the right UX: the user may have made
// changes via another flow while the panel was hidden. We skip the very FIRST
// activation, though — Vue fires onActivated immediately after onMounted on
// initial mount, and a duplicate 7-store-fetch + dep-broker fetch in the same
// frame wastes a non-trivial amount of bandwidth. `startDepBrokerPoll` is
// already idempotent so it would no-op anyway.
let activatedOnce = false;
onActivated(() => {
  if (!activatedOnce) {
    activatedOnce = true;
    return;
  }
  loadOverview();
  startDepBrokerPoll();
});

onDeactivated(stopDepBrokerPoll);
onBeforeUnmount(stopDepBrokerPoll);

function refreshAll(): void {
  void store.fetchPolicy();
  void store.fetchPending();
  void store.fetchProcessGrants();
  void store.fetchSessionGrants();
  void store.fetchPolicyTemplates();
  void store.fetchSkillDiscovery();
  void store.fetchHealth();
  void depBroker.fetchPending();
}

async function toggleDepBroker(): Promise<void> {
  await depBroker.setEnabled(!depBroker.settings.value.enabled);
}

// ── FileGuard status card toggles (run-mode / dynamic-auth) ──
// The master on/off is driven solely by the 3-state master-mode segment
// (`onSetSecurityMode`) — the backend keeps `disabled ⟺ policy.enabled=false`
// in lock-step, so a separate `enabled` bool toggle was redundant and removed.

async function setMode(next: "enforce" | "audit_only"): Promise<void> {
  // Defensive: the run-mode sub-switch is meaningless while the master
  // switch forces audit_only (permissive/disabled). The buttons are already
  // `:disabled` in that state; this guard keeps a stray call a no-op.
  if (runModeDisabled.value) return;
  if (next === "audit_only") {
    const ok = await confirm({
      icon: "⚠️",
      title: t("security.runModeLabel"),
      message: t("security.runModeDesc").replace(/<\/?b>/g, ""),
      confirmStyle: "danger",
      confirmText: t("common.confirm"),
      cancelText: t("common.cancel"),
    });
    if (!ok) return;
  }
  await store.updatePolicyToggles({ mode: next });
}

async function toggleDynamicAuth(next: boolean): Promise<void> {
  await store.updatePolicyToggles({ dynamic_authorization: next });
}

// ── Export audit log as CSV (V1 exportAudit) ───────────────────────────

async function exportAudit(): Promise<void> {
  await store.fetchAudit();
  const rows = store.auditEntries;
  const header = ["time", "decision", "subject", "resource", "rule_id", "note"];
  const escape = (v: string): string => `"${(v ?? "").replace(/"/g, '""')}"`;
  const csv = [
    header.join(","),
    ...rows.map((e) =>
      [
        e.occurred_at,
        e.decision,
        e.subject.identifier,
        e.resource.identifier,
        e.rule_id ?? "",
        e.note ?? "",
      ]
        .map((v) => escape(String(v)))
        .join(","),
    ),
  ].join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  a.href = url;
  a.download = `fileguard-audit-${stamp}.csv`;
  document.body.appendChild(a);
  a.click();
  window.setTimeout(() => {
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, 100);
}

defineExpose({ refreshAll });
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- v-html renders only our own static, trusted i18n catalog strings (no user/remote input); not an XSS vector. -->
  <div class="security-section">
    <!-- V1-parity (SecurityConfigPanel.js:999-1002): loading spinner when policy not yet loaded -->
    <div
      v-if="!store.policy"
      class="sec-cfg-empty"
    >
      <div
        class="spinner"
        style="width:24px;height:24px;border-width:3px"
      />
      <span style="margin-left:10px">{{ t('security.loadingPolicy') }}</span>
    </div>
    <template v-else>
      <!-- Security master switch (3c switch-tree §6.4): the NEW 3-state
         master control (enforcing / permissive / disabled). ORTHOGONAL to
         the enforce/audit_only run-mode sub-switch below (different value
         domain — do not conflate). Bound to `PUT /api/security/mode`. -->
      <div class="sec-master-mode">
        <div class="sec-master-mode__head">
          <div class="sec-master-mode__title">
            🛡️ {{ t('security.masterMode.label') }}
          </div>
          <div class="sec-master-mode__desc">
            {{ t('security.masterMode.desc') }}
          </div>
        </div>
        <div
          class="sec-master-mode__seg"
          role="radiogroup"
          :aria-label="t('security.masterMode.label')"
        >
          <button
            v-for="m in (['enforcing', 'permissive', 'disabled'] as const)"
            :key="m"
            type="button"
            class="sec-master-mode__btn"
            :class="{ 'sec-master-mode__btn--active': securityMode === m }"
            :aria-checked="securityMode === m"
            role="radio"
            :title="t(`security.masterMode.state.${m}Title`)"
            @click="onSetSecurityMode(m)"
          >
            {{ t(`security.masterMode.state.${m}`) }}
          </button>
        </div>
        <div class="sec-master-mode__hint">
          {{ t(`security.masterMode.hint.${securityMode}`) }}
        </div>
      </div>

      <div class="sec-cfg-perm-card">
        <div class="sec-cfg-perm-title">
          {{ t('security.overview.title') }}
        </div>

        <!-- Read -->
        <div class="sec-cfg-perm-row">
          <span class="sec-cfg-perm-icon">📖</span>
          <div class="sec-cfg-perm-body">
            <div
              class="sec-cfg-perm-collapsible-hdr"
              @click="overview.toggle('read')"
            >
              <span class="sec-cfg-perm-label">{{ t('security.overview.read') }}</span>
              <span class="sec-cfg-perm-count-badge">{{ overview.permissionSummary.value.totalRead }} {{ t('security.overview.countSuffix') }}</span>
              <span
                v-if="overview.permissionSummary.value.hasInactiveRead"
                class="sec-cfg-perm-count-badge sec-cfg-perm-count-badge--dim"
              >{{ t('security.overview.inactive') }}</span>
              <span class="sec-cfg-perm-chevron">{{ overview.expanded.value.read ? '▲' : '▼' }}</span>
            </div>
            <div v-show="overview.expanded.value.read">
              <div class="sec-cfg-perm-skill-group-hdr">
                <span class="sec-cfg-perm-skill-group-name">{{ t('security.overview.basePolicy') }}</span>
              </div>
              <div
                class="sec-cfg-perm-paths"
                style="margin-bottom: 6px;"
              >
                <span
                  v-for="(p, idx) in overview.permissionSummary.value.baseAllowed"
                  :key="'br-' + idx"
                  class="sec-cfg-perm-path"
                >{{ p }}</span>
                <span
                  v-if="overview.permissionSummary.value.baseAllowed.length === 0"
                  class="sec-cfg-perm-empty"
                >{{ t('security.overview.unconfigured') }}</span>
              </div>
              <template
                v-for="grp in overview.permissionSummary.value.read"
                :key="'rg-' + grp.skillName"
              >
                <div class="sec-cfg-perm-skill-group-hdr">
                  <span class="sec-cfg-perm-skill-group-name">{{ grp.skillName }}</span>
                  <span
                    class="sec-cfg-skill-badge"
                    :class="grp.active ? 'sec-cfg-skill-badge--active' : 'sec-cfg-skill-badge--inactive'"
                  >{{ grp.active ? t('security.overview.active') : t('security.overview.notActive') }}</span>
                </div>
                <div
                  class="sec-cfg-perm-paths"
                  style="margin-bottom: 6px;"
                >
                  <span
                    v-for="(p, pi) in grp.paths"
                    :key="'rgp-' + grp.skillName + '-' + pi"
                    class="sec-cfg-perm-path"
                    :class="{ 'sec-cfg-perm-path--dimmed': !grp.active }"
                    :title="t('security.overview.fromSkill', { name: grp.skillName })"
                  >{{ p }}</span>
                </div>
              </template>
            </div>
          </div>
        </div>

        <!-- Write -->
        <div class="sec-cfg-perm-row">
          <span class="sec-cfg-perm-icon">✏️</span>
          <div class="sec-cfg-perm-body">
            <div
              class="sec-cfg-perm-collapsible-hdr"
              @click="overview.toggle('write')"
            >
              <span class="sec-cfg-perm-label">{{ t('security.overview.write') }}</span>
              <span class="sec-cfg-perm-count-badge">{{ overview.permissionSummary.value.totalWrite }} {{ t('security.overview.countSuffix') }}</span>
              <span
                v-if="overview.permissionSummary.value.hasInactiveWrite"
                class="sec-cfg-perm-count-badge sec-cfg-perm-count-badge--dim"
              >{{ t('security.overview.inactive') }}</span>
              <span class="sec-cfg-perm-chevron">{{ overview.expanded.value.write ? '▲' : '▼' }}</span>
            </div>
            <div v-show="overview.expanded.value.write">
              <div class="sec-cfg-perm-skill-group-hdr">
                <span class="sec-cfg-perm-skill-group-name">{{ t('security.overview.basePolicy') }}</span>
              </div>
              <div
                class="sec-cfg-perm-paths"
                style="margin-bottom: 6px;"
              >
                <span
                  v-for="(p, idx) in overview.permissionSummary.value.baseAllowed"
                  :key="'bw-' + idx"
                  class="sec-cfg-perm-path"
                >{{ p }}</span>
                <span
                  v-if="overview.permissionSummary.value.baseAllowed.length === 0"
                  class="sec-cfg-perm-empty"
                >{{ t('security.overview.unconfigured') }}</span>
              </div>
              <template
                v-for="grp in overview.permissionSummary.value.write"
                :key="'wg-' + grp.skillName"
              >
                <div class="sec-cfg-perm-skill-group-hdr">
                  <span class="sec-cfg-perm-skill-group-name">{{ grp.skillName }}</span>
                  <span
                    class="sec-cfg-skill-badge"
                    :class="grp.active ? 'sec-cfg-skill-badge--active' : 'sec-cfg-skill-badge--inactive'"
                  >{{ grp.active ? t('security.overview.active') : t('security.overview.notActive') }}</span>
                </div>
                <div
                  class="sec-cfg-perm-paths"
                  style="margin-bottom: 6px;"
                >
                  <span
                    v-for="(p, pi) in grp.paths"
                    :key="'wgp-' + grp.skillName + '-' + pi"
                    class="sec-cfg-perm-path"
                    :class="{ 'sec-cfg-perm-path--dimmed': !grp.active }"
                    :title="t('security.overview.fromSkill', { name: grp.skillName })"
                  >{{ p }}</span>
                </div>
              </template>
            </div>
          </div>
        </div>

        <!-- Run -->
        <div class="sec-cfg-perm-row">
          <span class="sec-cfg-perm-icon">▶️</span>
          <div class="sec-cfg-perm-body">
            <div
              class="sec-cfg-perm-collapsible-hdr"
              @click="overview.toggle('run')"
            >
              <span class="sec-cfg-perm-label">{{ t('security.overview.run') }}</span>
              <span
                v-if="overview.permissionSummary.value.totalRun > 0"
                class="sec-cfg-perm-count-badge"
              >{{ overview.permissionSummary.value.totalRun }} {{ t('security.overview.countSuffix') }}</span>
              <!-- V1-parity (SecurityConfigPanel.js:1094-1095): chevron shown when
                 trustedBinaries OR workspaceDirs has entries -->
              <span
                v-if="overview.permissionSummary.value.run.length > 0 || overview.permissionSummary.value.workspaceDirs.length > 0"
                class="sec-cfg-perm-chevron"
              >{{ overview.expanded.value.run ? '▲' : '▼' }}</span>
            </div>
            <!-- V1-parity (SecurityConfigPanel.js:1097-1098): empty state only when
               BOTH trustedBinaries AND workspaceDirs are empty -->
            <div
              v-if="overview.permissionSummary.value.run.length === 0 && overview.permissionSummary.value.workspaceDirs.length === 0"
              class="sec-cfg-perm-empty"
            >
              {{ t('security.overview.noTrustedPrograms') }}
            </div>
            <div v-show="overview.expanded.value.run">
              <!-- V1-parity (SecurityConfigPanel.js:1100-1102): workspace dirs note -->
              <div
                v-if="overview.permissionSummary.value.workspaceDirs.length > 0"
                class="sec-cfg-perm-note"
                style="margin-bottom: 4px;"
              >
                + {{ overview.permissionSummary.value.workspaceDirs.join('、') }} {{ t('security.overview.workspaceNote') }}
              </div>
              <template
                v-for="grp in overview.permissionSummary.value.run"
                :key="'tg-' + grp.skillName"
              >
                <div class="sec-cfg-perm-skill-group-hdr">
                  <span class="sec-cfg-perm-skill-group-name">{{ grp.skillName }}</span>
                  <span
                    class="sec-cfg-skill-badge"
                    :class="grp.active ? 'sec-cfg-skill-badge--active' : 'sec-cfg-skill-badge--inactive'"
                  >{{ grp.active ? t('security.overview.active') : t('security.overview.notActive') }}</span>
                </div>
                <div
                  class="sec-cfg-perm-paths"
                  style="margin-bottom: 6px;"
                >
                  <span
                    v-for="(p, pi) in grp.paths"
                    :key="'tgp-' + grp.skillName + '-' + pi"
                    class="sec-cfg-perm-path sec-cfg-perm-path--trusted"
                    :class="{ 'sec-cfg-perm-path--dimmed': !grp.active }"
                    :title="t('security.overview.fromSkill', { name: grp.skillName })"
                  >{{ p }}</span>
                </div>
              </template>
            </div>
          </div>
        </div>

        <!-- Requires approval / denied (V1 1122-1129) -->
        <div class="sec-cfg-perm-row sec-cfg-perm-row--ask">
          <span class="sec-cfg-perm-icon">{{ overview.permissionSummary.value.dynamicAuth ? '❓' : '🚫' }}</span>
          <div class="sec-cfg-perm-body">
            <div class="sec-cfg-perm-label">
              {{ overview.permissionSummary.value.dynamicAuth ? t('security.overview.ask') : t('security.overview.deny') }}
            </div>
            <div class="sec-cfg-perm-note">
              {{ t('security.overview.outsideScope') }}
            </div>
          </div>
        </div>
      </div>

      <!-- 2. Dry-run tester (V1 1132-1164) -->
      <div class="sec-cfg-checker">
        <div class="sec-cfg-checker-title">
          {{ t('security.checker.title') }}
        </div>
        <div class="sec-cfg-checker-row">
          <select
            v-model="overview.dryRunOp.value"
            class="sec-cfg-checker-op"
          >
            <option value="execute">
              {{ t('security.checker.run') }}
            </option>
            <option value="read">
              {{ t('security.checker.read') }}
            </option>
            <option value="write">
              {{ t('security.checker.write') }}
            </option>
          </select>
          <input
            v-model="overview.dryRunPath.value"
            type="text"
            class="sec-cfg-checker-input mono"
            :placeholder="overview.dryRunOp.value === 'execute' ? t('security.checker.placeholder.exec') : t('security.checker.placeholder.file')"
            @keyup.enter="overview.runDryRun"
          />
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            :disabled="overview.dryRunLoading.value || !overview.dryRunPath.value.trim()"
            @click="overview.runDryRun"
          >
            {{ overview.dryRunLoading.value ? '…' : t('security.checker.check') }}
          </button>
        </div>
        <div
          v-if="overview.dryRunResult.value"
          class="sec-cfg-checker-result"
          :class="`sec-cfg-checker-result--${overview.dryRunResult.value.decision}`"
        >
          <div class="sec-cfg-checker-verdict">
            <span v-if="overview.dryRunResult.value.decision === 'allow'">{{ t('security.checker.allow') }}</span>
            <span v-else-if="overview.dryRunResult.value.decision === 'ask'">{{ t('security.checker.ask') }}</span>
            <span v-else-if="overview.dryRunResult.value.decision === 'deny'">{{ t('security.checker.deny') }}</span>
            <span v-else>{{ t('security.checker.error') }}</span>
            <span class="sec-cfg-checker-reason mono">{{ overview.dryRunResult.value.reason }}</span>
          </div>
          <!-- V1 parity (SecurityConfigPanel.js:1159-1162): matched_rule + explanation detail. -->
          <div
            v-if="overview.dryRunResult.value.matchedRule"
            class="sec-check-detail"
          >
            <div>
              <strong>{{ t('simulator.matchedRule') }}:</strong>
              {{ overview.dryRunResult.value.matchedRule.type }} ({{ overview.dryRunResult.value.matchedRule.source }})
            </div>
            <div v-if="overview.dryRunResult.value.explanation">
              <strong>{{ t('simulator.explanation') }}:</strong>
              {{ overview.dryRunResult.value.explanation }}
            </div>
          </div>
        </div>
      </div>

      <!-- 3. Policy Templates (V1 1166-1200) -->
      <div class="sec-cfg-status-card">
        <div class="sec-cfg-block-header">
          <div class="sec-cfg-block-title">
            📋 {{ t('policyTemplates.title') }}
          </div>
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            :disabled="store.loadingTemplates"
            @click="store.fetchPolicyTemplates()"
          >
            ↺ {{ t('common.refresh') }}
          </button>
        </div>
        <div class="sec-cfg-list-desc">
          {{ t('policyTemplates.desc') }}
        </div>
        <EmptyState
          v-if="store.policyTemplates.length === 0"
          :body="t('policyTemplates.empty')"
        />
        <template v-else>
          <div
            v-for="tpl in store.policyTemplates"
            :key="tpl.id"
            class="sec-cfg-status-row"
          >
            <div class="sec-cfg-status-label">
              <div class="sec-cfg-status-name">
                {{ tpl.name }}
              </div>
              <div class="sec-cfg-status-hint">
                {{ tpl.description }}
              </div>
            </div>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              :disabled="store.applyingTemplate !== null"
              @click="store.applyTemplate(tpl.id)"
            >
              {{ store.applyingTemplate === tpl.id ? '…' : t('policyTemplates.apply') }}
            </button>
          </div>
        </template>
      </div>

      <!-- 4. Dependency Broker -->
      <div class="sec-cfg-status-card">
        <div class="sec-cfg-block-header">
          <div class="sec-cfg-block-title">
            🛡️ {{ t('depBroker.title') }}
          </div>
          <div class="sec-depbroker-controls">
            <label class="sec-depbroker-switch">
              <input
                type="checkbox"
                :checked="depBroker.settings.value.enabled"
                @change="toggleDepBroker"
              />
              <span>{{ depBroker.settings.value.enabled ? t('depBroker.enabled') : t('common.disabled') }}</span>
            </label>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              :disabled="depBroker.loading.value"
              @click="depBroker.fetchPending()"
            >
              ↺ {{ t('common.refresh') }}
            </button>
          </div>
        </div>
        <div class="sec-cfg-list-desc">
          {{ t('depBroker.description') }}
        </div>
        <EmptyState
          v-if="depBroker.pending.value.length === 0"
          :body="t('depBroker.pending.empty')"
        />
        <template v-else>
          <div
            v-for="req in depBroker.pending.value"
            :key="req.id"
            class="sec-cfg-pending-row"
          >
            <div class="sec-cfg-pending-info">
              <code class="mono">{{ req.command_args.join(' ') }}</code>
              <span class="sec-cfg-grant-meta">{{ req.requester }} · {{ req.status }}</span>
            </div>
            <div class="sec-cfg-actions-row">
              <button
                type="button"
                class="btn btn-sm btn-primary"
                @click="depBroker.approve(req.id)"
              >
                {{ t('depBroker.approve') }}
              </button>
              <button
                type="button"
                class="btn btn-sm btn-danger"
                @click="depBroker.reject(req.id)"
              >
                {{ t('depBroker.reject') }}
              </button>
            </div>
          </div>
        </template>
      </div>

      <!-- 5. FileGuard status card (V1 1223-1272) -->
      <section
        v-if="store.policy"
        class="sec-cfg-status-card"
      >
        <!-- Native sub-process hook status (READ-ONLY; 🔴 State-Truth-First).
           Surfaces the real guard64.dll hook state so "Python on but DLL
           failed to load" shows as 降级/degraded instead of "healthy". -->
        <div
          v-if="store.policy.enabled"
          class="sec-cfg-status-row"
        >
          <div class="sec-cfg-status-label">
            <div class="sec-cfg-status-name">
              {{ t('security.nativeHook.label') }}
            </div>
            <div class="sec-cfg-status-hint">
              {{ t('security.nativeHook.desc') }}
            </div>
          </div>
          <span
            class="sec-native-status"
            :class="`sec-native-status--${nativeHookState}`"
          >
            {{ t(`security.nativeHook.state.${nativeHookState}`) }}
          </span>
        </div>

        <div class="sec-cfg-status-row">
          <div class="sec-cfg-status-label">
            <div class="sec-cfg-status-name">
              {{ t('security.runModeLabel') }}
            </div>
            <div
              class="sec-cfg-status-hint"
              v-html="t('security.runModeDesc')"
            ></div>
          </div>
          <div
            class="sec-cfg-mode-switch"
            :class="{ 'sec-cfg-mode-switch--disabled': runModeDisabled }"
            :title="runModeDisabled ? t('security.masterMode.runModeLockedTitle') : ''"
          >
            <button
              type="button"
              class="sec-cfg-mode-btn"
              :class="{ active: store.policy.mode === 'enforce' }"
              :disabled="runModeDisabled"
              @click="setMode('enforce')"
            >
              enforce
            </button>
            <button
              type="button"
              class="sec-cfg-mode-btn"
              :class="{ active: store.policy.mode === 'audit_only' }"
              :disabled="runModeDisabled"
              @click="setMode('audit_only')"
            >
              audit_only
            </button>
          </div>
        </div>

        <div class="sec-cfg-status-row">
          <div class="sec-cfg-status-label">
            <div class="sec-cfg-status-name">
              {{ t('security.dynamicAuthLabel') }}
            </div>
            <div class="sec-cfg-status-hint">
              {{ t('security.dynamicAuthDesc') }}
            </div>
          </div>
          <label class="toggle">
            <input
              type="checkbox"
              :checked="store.policy.dynamic_authorization"
              @change="toggleDynamicAuth(($event.target as HTMLInputElement).checked)"
            />
            <span class="toggle-slider"></span>
          </label>
        </div>

      </section>

      <!-- 6. IM channel authorization (V1 1274-1346) -->
      <section class="sec-cfg-status-card">
        <div class="sec-cfg-block-title">
          📱 {{ t('security.imChannels.sectionTitle') }}
        </div>
        <div class="sec-cfg-list-desc">
          {{ t('security.imChannels.sectionDesc') }}
        </div>
        <div class="sec-cfg-status-row">
          <div class="sec-cfg-status-label">
            <div class="sec-cfg-status-name sec-channel-name">
              <span class="sec-channel-tag sec-channel-tag--wechat">wechat</span>
              {{ t('security.imChannels.wechat') }}
            </div>
            <div class="sec-cfg-status-hint">
              {{ t('security.imChannels.enableDialogHint') }}
              <span
                v-if="overview.wechatDialogEnabled.value"
                class="sec-channel-on"
              >· {{ t('security.imChannels.enabledHint') }}</span>
              <span
                v-else
                class="sec-channel-off"
              >· {{ t('security.imChannels.disabledHint') }}</span>
            </div>
          </div>
          <label class="toggle">
            <input
              type="checkbox"
              :checked="overview.wechatDialogEnabled.value"
              @change="overview.toggleImChannelDialog('wechat', ($event.target as HTMLInputElement).checked)"
            />
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div class="sec-cfg-status-row">
          <div class="sec-cfg-status-label">
            <div class="sec-cfg-status-name sec-channel-name">
              <span class="sec-channel-tag sec-channel-tag--feishu">feishu</span>
              {{ t('security.imChannels.feishu') }}
            </div>
            <div class="sec-cfg-status-hint">
              {{ t('security.imChannels.enableDialogHint') }}
              <span
                v-if="overview.feishuDialogEnabled.value"
                class="sec-channel-on"
              >· {{ t('security.imChannels.enabledHint') }}</span>
              <span
                v-else
                class="sec-channel-off"
              >· {{ t('security.imChannels.disabledHint') }}</span>
            </div>
          </div>
          <label class="toggle">
            <input
              type="checkbox"
              :checked="overview.feishuDialogEnabled.value"
              @change="overview.toggleImChannelDialog('feishu', ($event.target as HTMLInputElement).checked)"
            />
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div class="sec-grant-cmd">
          <div class="sec-grant-cmd__title">
            🔑 {{ t('security.imChannels.grantCmdTitle') }}
          </div>
          <div class="sec-grant-cmd__desc">
            {{ t('security.imChannels.grantCmdDesc') }}
          </div>
          <div class="sec-grant-cmd__box mono">
            <div>{{ t('security.imChannels.grantCmdRead') }}</div>
            <div>{{ t('security.imChannels.grantCmdWrite') }}</div>
            <div>{{ t('security.imChannels.grantCmdExec') }}</div>
            <div>{{ t('security.imChannels.grantCmdList') }}</div>
            <div>{{ t('security.imChannels.grantCmdRevoke') }}</div>
          </div>
          <div class="sec-grant-cmd__note">
            ⚠️ {{ t('security.imChannels.grantCmdNote') }}
          </div>
        </div>
      </section>

      <!-- 7. Stat tiles (V1 1348-1366: pending / session / process / skill).
         Field mapping must match the label below each tile:
           - tile 2 → `sessionGrants` (`security.sessionGrantsLabel`)
           - tile 3 → `processGrants` (`security.processGrantsLabel`)
         Previously the renderings were swapped (and tile 3 was bound to
         `depBroker.pending` which is unrelated to either grant set), so the
         operator saw a number with the wrong meaning under each label. -->
      <div class="sec-cfg-tiles">
        <div class="sec-cfg-tile">
          <div class="sec-cfg-tile-num">
            {{ store.pendingRequests.length }}
          </div>
          <div class="sec-cfg-tile-label">
            {{ t('security.pendingRequestsLabel') }}
          </div>
        </div>
        <div class="sec-cfg-tile">
          <div class="sec-cfg-tile-num">
            {{ store.sessionGrants.length }}
          </div>
          <div class="sec-cfg-tile-label">
            {{ t('security.sessionGrantsLabel') }}
          </div>
        </div>
        <div class="sec-cfg-tile">
          <div class="sec-cfg-tile-num">
            {{ store.processGrants.length }}
          </div>
          <div class="sec-cfg-tile-label">
            {{ t('security.processGrantsLabel') }}
          </div>
        </div>
        <div class="sec-cfg-tile">
          <div class="sec-cfg-tile-num">
            {{ store.discoveredSkills.length }}
          </div>
          <div class="sec-cfg-tile-label">
            {{ t('security.skillPoliciesLabel') }}
          </div>
        </div>
      </div>

      <!-- 8. Actions (V1 1368-1374) -->
      <div class="sec-cfg-actions-row">
        <button
          type="button"
          class="btn btn-ghost"
          :disabled="store.auditEntries.length === 0"
          @click="exportAudit"
        >
          {{ t('security.exportAuditBtn') }}
        </button>
        <button
          type="button"
          class="btn btn-ghost"
          @click="emit('view-audit')"
        >
          {{ t('security.viewFullAuditBtn') }}
        </button>
      </div>
    </template><!-- end v-else (policy loaded) -->
  </div>
  <!-- eslint-enable vue/no-v-html -->
</template>

<style>
/* ── Security master switch (3c §6.4) — 3-state segmented control ─────── */
.sec-master-mode {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  margin-bottom: var(--space-3);
  background: var(--bg-code);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.sec-master-mode__title {
  font-size: var(--text-sm);
  font-weight: 700;
  color: var(--text-primary);
}
.sec-master-mode__desc {
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-top: 2px;
}
.sec-master-mode__seg {
  display: inline-flex;
  gap: 4px;
  background: var(--bg-tertiary);
  border-radius: 6px;
  padding: 3px;
  width: fit-content;
}
.sec-master-mode__btn {
  border: none;
  background: none;
  cursor: pointer;
  font-size: var(--text-sm);
  padding: 4px 14px;
  border-radius: 4px;
  color: var(--text-secondary);
  transition: background 0.15s, color 0.15s;
}
.sec-master-mode__btn:hover {
  color: var(--text-primary);
}
.sec-master-mode__btn--active {
  background: var(--accent);
  color: #fff;
}
.sec-master-mode__hint {
  font-size: var(--text-xs);
  color: var(--text-muted);
  font-style: italic;
}

/* ── run-mode sub-switch: greyed out while master forces audit_only ───── */
/* Reuses the P-11 native-disabled visual language (reduced opacity +
   not-allowed cursor). The buttons themselves are `:disabled`; this styles
   the wrapper so the whole control reads as inactive. */
.sec-cfg-mode-switch--disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.sec-cfg-mode-switch--disabled .sec-cfg-mode-btn {
  cursor: not-allowed;
  pointer-events: none;
}

/* ── Dependency Broker controls ──────────────────────────────────────── */
.sec-depbroker-controls {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.sec-depbroker-switch {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-sm);
  cursor: pointer;
  user-select: none;
}

/* ── IM channel tags + on/off hints (token-based) ────────────────────── */
.sec-channel-name {
  display: flex;
  align-items: center;
  gap: 6px;
}
.sec-channel-tag {
  padding: 1px 6px;
  border-radius: 4px;
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.sec-channel-tag--wechat {
  background: rgba(52, 211, 153, 0.15);
  color: var(--success);
}
.sec-channel-tag--feishu {
  background: rgba(168, 85, 247, 0.15);
  color: var(--accent);
}
.sec-channel-on { color: var(--success); margin-left: 4px; }
.sec-channel-off { color: var(--text-muted); margin-left: 4px; }

/* ── Native sub-process hook status pill (read-only) ─────────────────── */
.sec-native-status {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: var(--text-xs);
  font-weight: 600;
  white-space: nowrap;
}
.sec-native-status--active {
  background: rgba(52, 211, 153, 0.15);
  color: var(--success);
}
.sec-native-status--degraded {
  background: rgba(251, 191, 36, 0.15);
  color: var(--warning);
}
.sec-native-status--off {
  background: var(--bg-code);
  color: var(--text-muted);
}

/* ── /grant command help box ─────────────────────────────────────────── */
.sec-grant-cmd {
  margin-top: var(--space-3);
  padding-top: var(--space-3);
  border-top: 1px solid var(--border);
}
.sec-grant-cmd__title {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 6px;
}
.sec-grant-cmd__desc {
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-bottom: 6px;
}
.sec-grant-cmd__box {
  font-size: var(--text-xs);
  background: var(--bg-code);
  border-radius: 6px;
  padding: 8px 10px;
  line-height: 1.8;
  color: var(--text-primary);
}
.sec-grant-cmd__note {
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-top: 6px;
  font-style: italic;
}
</style>
