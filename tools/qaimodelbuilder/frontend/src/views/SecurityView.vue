<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Security view — 7-tab layout (Overview / Tool Safety / Allow Lists /
 * Audit-Grants / Skill Capabilities / Project Directory / Auto-Approve).
 *
 * Uses global CSS from security.css (.security-panel, .security-tabs, .security-tab).
 * Mounts dedicated panel components per tab — including the Overview tab,
 * which now lives in `SecurityOverviewPanel`. This view is a thin tab
 * container: header (FileGuard status + refresh) + tab nav + per-tab
 * <Panel> dispatch.
 */
import { ref, computed, defineAsyncComponent } from "vue";
import { useI18n } from "vue-i18n";
import { useSecurityStore } from "@/stores/security";
import { useHeaderActions } from "@/composables/useHeaderActions";
import { ICON_REFRESH } from "@/components/icons/topbarIcons";
import UiTabs from "@/components/chat/UiTabs.vue";
// Overview is the default tab (first paint), so it stays a static import.
import SecurityOverviewPanel from "@/components/security/SecurityOverviewPanel.vue";
// The other 6 tab panels are only mounted (via v-else-if) once the user
// switches to their tab, so they are code-split into their own chunks via
// defineAsyncComponent — they no longer inflate the SecurityView entry chunk.
// SecurityConfigPanel keeps a typed InstanceType ref (header unsaved/saveStatus
// hint reads its exposed members), which defineAsyncComponent preserves.
const SecurityConfigPanel = defineAsyncComponent(
  () => import("@/components/security/SecurityConfigPanel.vue"),
);
const AutoApprovePanel = defineAsyncComponent(
  () => import("@/components/security/AutoApprovePanel.vue"),
);
const ProjectAccessPanel = defineAsyncComponent(
  () => import("@/components/security/ProjectAccessPanel.vue"),
);
const AuditLogPanel = defineAsyncComponent(
  () => import("@/components/security/AuditLogPanel.vue"),
);
const SkillCapabilitiesPanel = defineAsyncComponent(
  () => import("@/components/security/SkillCapabilitiesPanel.vue"),
);
const ToolSafetyPanel = defineAsyncComponent(
  () => import("@/components/security/ToolSafetyPanel.vue"),
);

type SecurityTab =
  | "overview"
  | "tool-safety"
  | "allow-lists"
  | "audit"
  | "skill"
  | "project-dir"
  | "auto-approve";

interface TabDef {
  id: SecurityTab;
  label: string;
  badge?: string | number;
  dot?: boolean;
}

const { t } = useI18n();
const store = useSecurityStore();
const activeTab = ref<SecurityTab>("overview");

/**
 * Template ref to the SecurityConfigPanel (Allow Lists tab).
 * Used to read hasUnsavedChanges / saveStatus for the header-level
 * unsaved hint and saveStatus message (V1 SecurityConfigPanel.js:959-970).
 */
const securityConfigPanelRef = ref<InstanceType<typeof SecurityConfigPanel> | null>(null);

/**
 * Template ref to the Overview panel. The header "↺ refresh" button drives
 * its `refreshAll()` (V1 parity). The ref is only populated while the
 * Overview tab is active (v-if), so the optional-chained call is a no-op on
 * other tabs — matching the previous behaviour where refresh served the
 * Overview's store-backed data.
 */
const overviewPanelRef = ref<InstanceType<typeof SecurityOverviewPanel> | null>(null);

// Register the top-most title-bar refresh action (icon button in the global
// topbar), matching Settings / Skills / Service which all use the shared
// `useHeaderActions` + `ICON_REFRESH` pattern. This replaces the divergent
// in-body "↺ 刷新" text button that used to sit on the FileGuard sub-header.
// Behaviour is preserved: it drives the Overview panel's `refreshAll()` (a
// no-op on other tabs, where the ref is null — same as before).
useHeaderActions(() => [
  {
    id: "security.refresh",
    label: t("common.refresh"),
    iconSvg: ICON_REFRESH,
    title: t("common.refresh"),
    testId: "security-refresh-btn",
    onClick: () => {
      overviewPanelRef.value?.refreshAll();
    },
  },
]);

/** Whether the Allow Lists tab has unsaved changes (V1 hasUnsavedListChanges). */
const hasUnsavedListChanges = computed<boolean>(
  () => securityConfigPanelRef.value?.hasUnsavedChanges ?? false,
);

/** Save status from the Allow Lists panel (V1 saveStatus). */
const allowListsSaveStatus = computed<"" | "success" | "error">(
  () => securityConfigPanelRef.value?.saveStatus ?? "",
);

const tabs = computed<TabDef[]>(() => [
  {
    id: "overview",
    label: t("security.tabOverview"),
  },
  {
    id: "tool-safety",
    label: t("toolSafety.title"),
  },
  {
    id: "allow-lists",
    label: t("security.tabAllowLists"),
    // V1-parity (SecurityConfigPanel.js:980-982): orange dot when there are
    // unsaved list changes.
    dot: hasUnsavedListChanges.value,
  },
  {
    id: "audit",
    label: t("security.tabAudit"),
    // V1-parity (SecurityConfigPanel.js:984-986): pending requests count badge.
    badge: store.pendingRequests.length > 0 ? store.pendingRequests.length : undefined,
  },
  { id: "skill", label: t("security.tabSkills") },
  { id: "project-dir", label: t("projectAccess.title") },
  { id: "auto-approve", label: t("security.tabAutoApprove") },
]);

/** Bridge UiTabs' generic string v-model to the typed SecurityTab ref. */
const activeTabModel = computed<string>({
  get: () => activeTab.value,
  set: (v) => {
    activeTab.value = v as SecurityTab;
  },
});
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- v-html renders only our own static, trusted i18n catalog strings (no user/remote input); not an XSS vector. -->
  <section
    class="panel-view security-panel"
    :aria-label="t('views.security.title')"
  >
    <!-- Health banner: shown when down / test_mode (V1 922-940) -->
    <div
      v-if="store.health.status === 'down'"
      class="sec-cfg-health-banner sec-cfg-health-banner--down"
    >
      <span class="sec-cfg-health-icon">❌</span>
      <div class="sec-cfg-health-text">
        <b>{{ t('security.healthBannerDownTitle') }}</b>
        {{ t('security.healthBannerDownDesc') }}
        <span class="sec-cfg-health-hint">{{ t('security.healthBannerDownHint') }}</span>
      </div>
    </div>
    <div
      v-else-if="store.health.status === 'test_mode'"
      class="sec-cfg-health-banner sec-cfg-health-banner--warn"
    >
      <span class="sec-cfg-health-icon">⚠</span>
      <div class="sec-cfg-health-text">
        <b>{{ t('security.healthBannerTestTitle') }}</b>
        {{ t('security.healthBannerTestDesc') }}
        <span class="sec-cfg-health-hint">{{ t('security.healthBannerTestHint') }}</span>
      </div>
    </div>

    <!-- FileGuard header with mode/health status -->
    <div class="sec-header">
      <div class="sec-header-left">
        <span class="sec-header-icon">🛡️</span>
        <h2 class="sec-header-title">
          {{ t('security.panelTitle') }}
        </h2>
      </div>
      <div class="sec-header-meta">
        <template v-if="store.policy">
          <span>{{ t('security.modeLabel') }}<strong>{{ store.policy.mode || '?' }}</strong></span>
          <span>·</span>
          <span>{{ store.policy.enabled ? t('security.fileGuardEnabled') : t('security.fileGuardDisabled') }}</span>
          <span>·</span>
          <span>{{ t('security.healthLabel') }}</span>
          <span
            v-if="store.health.status === 'ok'"
            class="sec-cfg-health-pill sec-cfg-health-pill--ok"
          >{{ t('security.healthOk') }}</span>
          <span
            v-else-if="store.health.status === 'down'"
            class="sec-cfg-health-pill sec-cfg-health-pill--down"
          >{{ t('security.healthDown') }}</span>
          <span
            v-else-if="store.health.status === 'test_mode'"
            class="sec-cfg-health-pill sec-cfg-health-pill--warn"
          >{{ t('security.healthTestMode') }}</span>
          <span
            v-else
            class="sec-cfg-health-pill sec-cfg-health-pill--unknown"
          >{{ t('security.healthDetecting') }}</span>
          <!-- V1-parity (SecurityConfigPanel.js:959-961): unsaved list changes hint -->
          <template v-if="hasUnsavedListChanges">
            <span>·</span>
            <span class="sec-cfg-unsaved">{{ t('security.unsaved') }}</span>
          </template>
        </template>
        <span v-else>{{ t('security.loadingPolicy') }}</span>
      </div>
      <!-- V1-parity (SecurityConfigPanel.js:968-970): saveStatus message.
           The refresh control moved to the global topbar (icon button,
           registered via useHeaderActions above) to match the Settings page,
           so it is no longer rendered here. -->
      <span
        v-if="allowListsSaveStatus"
        :class="['sec-cfg-status', `sec-cfg-status--${allowListsSaveStatus}`]"
      >
        {{ allowListsSaveStatus === 'success' ? t('security.config.saveStatusSuccess') : t('security.config.saveStatusError') }}
      </span>
    </div>

    <!-- Tab navigation -->
    <UiTabs
      v-model="activeTabModel"
      :tabs="tabs"
      variant="underline"
      :label="t('security.title')"
      class="security-tabs"
    />

    <!-- Tab content -->

    <!-- Overview -->
    <SecurityOverviewPanel
      v-if="activeTab === 'overview'"
      ref="overviewPanelRef"
      @view-audit="activeTab = 'audit'"
    />

    <!-- Tool Safety (three-layer security/tools switches) -->
    <div v-else-if="activeTab === 'tool-safety'">
      <ToolSafetyPanel />
    </div>

    <!-- Allow Lists -->
    <div v-else-if="activeTab === 'allow-lists'">
      <SecurityConfigPanel ref="securityConfigPanelRef" />
    </div>

    <!-- Audit -->
    <div v-else-if="activeTab === 'audit'">
      <AuditLogPanel />
    </div>

    <!-- Skill -->
    <div v-else-if="activeTab === 'skill'">
      <SkillCapabilitiesPanel />
    </div>

    <!-- Project Dir -->
    <div v-else-if="activeTab === 'project-dir'">
      <ProjectAccessPanel />
    </div>

    <!-- Auto-Approve -->
    <div v-else-if="activeTab === 'auto-approve'">
      <AutoApprovePanel />
    </div>
  </section>
  <!-- eslint-enable vue/no-v-html -->
</template>

<style>
/* ── FileGuard header (V1 panel-header parity) ───────────────────────── */
.sec-header {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin-bottom: var(--space-4);
  flex-wrap: wrap;
}
.sec-header-left {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.sec-header-icon { font-size: 1.5rem; }
.sec-header-title {
  font-size: var(--text-xl, 1.25rem);
  font-weight: 700;
  margin: 0;
}
.sec-header-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  flex: 1;
}
</style>
