<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Service view — GenieAPIService (model_runtime) control page.
 *
 * Full V1 parity (six regions), ported from the legacy template
 * (`frontend/index.html` `currentView === 'service'`) + its
 * `useServiceControl` composable, which is the validated behaviour source.
 *
 * Regions (matching V1):
 *   1. Connection bar (collapsible local port + Test probe) + local paths
 *   2. Path-safety warning banner
 *   3. Status card (running dot, PID/uptime, exe path, Start/Stop/Config)
 *   4. GenieAPIService-not-installed guidance card
 *   5. Launch Parameters (collapsible: model optgroups / port / log level
 *      + command preview)
 *   6. Logs (SSE stream, fullscreen toggle, scroll nav)
 *
 * Design vs V1: state lives in the cohesive `useServiceControl` composable;
 * this view is a thin, typed template. Reboot is NOT exposed on the service
 * panel (matching V1 — reboot lives only in the sidebar + reboot overlay).
 */
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";

import { useServiceControl } from "@/composables/useServiceControl";
import { useEscClose } from "@/composables/useClickOutside";
import { useDownloadsStore } from "@/stores/downloads";
import { useServiceStore } from "@/stores/service";
import { useToastStore } from "@/stores/toast";
import { useUiStore } from "@/stores/ui";
import { useHeaderActions } from "@/composables/useHeaderActions";
import ServiceLogPanel from "@/components/service/ServiceLogPanel.vue";
import ServiceConnectionBar from "@/components/service/ServiceConnectionBar.vue";
import ServicePathsSection from "@/components/service/ServicePathsSection.vue";
import ServicePathWarning from "@/components/service/ServicePathWarning.vue";
import ServiceStatusCard from "@/components/service/ServiceStatusCard.vue";
import ServiceLaunchParams from "@/components/service/ServiceLaunchParams.vue";
import ServiceConfigModal from "@/components/service/ServiceConfigModal.vue";
import {
  ICON_SETTINGS,
  ICON_REFRESH,
  ICON_DOCUMENT,
  ICON_TRASH,
} from "@/components/icons/topbarIcons";

const { t } = useI18n();
const router = useRouter();
const store = useServiceStore();
const ui = useUiStore();

// The log scroll container now lives inside ServiceLogPanel, which exposes
// its DOM ref. We hand a computed wrapper to useServiceControl so its
// auto-scroll logic keeps reading `.value` of the live node (V1 parity:
// same node, same behaviour — just owned by the child component now).
const logPanelComp = ref<InstanceType<typeof ServiceLogPanel> | null>(null);
const logPanelEl = computed<HTMLElement | null>(
  () => logPanelComp.value?.logPanel ?? null,
);
const svc = useServiceControl(logPanelEl, () => openConfigModal());

const {
  serviceStatus,
  serviceStarting,
  serviceStopping,
  serviceClearing,
  serviceLogs,
  serviceLogsStreaming,
  serviceModels,
  serviceModelsLoading,
  selectedServiceModelName,
  svcParams,
  svcParamsSaving,
  paramsCollapsed,
  connectionCollapsed,
  logsExpanded,
  connectionTesting,
  connectionTestResult,
  logUserScrolledUp,
  isRunning,
  serviceModelsByAccel,
  localUrl,
  serviceCommandPreview,
  canStartService,
  formatUptime,
} = svc;

// --- Download / install state (V1 `isAnyModelInstalling` / `isAnyModelDownloading`) ---
// V1 reads these from the shared Download Center state so the Service page can
// surface a "model preparing — view progress" hint while a download/install is
// in flight. V2 keeps the same shared singleton (`useDownloadsStore`), so we
// read its `isAnyModelDownloading` and derive `isAnyModelInstalling` from the
// aria2c install_status (V1 useDownloadCenter.js:106-108 — model-only install
// state; V2 equivalent is aria2c bootstrap install_status === "installing").
const downloads = useDownloadsStore();
const isAnyModelDownloading = downloads.isAnyModelDownloading;
// V1 isAnyModelInstalling: tracks model zip extraction/install in flight.
// V2 equivalent: aria2c install_status === "installing" (AppSidebar.vue:76).
const isAnyModelInstalling = computed<boolean>(
  () => downloads.aria2c.status.value.install_status === "installing",
);

// --- Service Config modal (rendered by ServiceConfigModal, which owns the
// mounted ServiceConfigPanel) ---
const configModalOpen = ref(false);
// V2 single-source-of-truth gate: the runtime `service_config.json` lives next
// to `GenieAPIService.exe`, so it can only be configured once the binary is
// installed. `exe_path` is empty when not installed; `serviceModelsLoading`
// guards the first-paint window so we don't flash "not installed" before the
// initial status probe resolves (same condition as the not-installed guidance
// card at the status-actions / notice blocks below).
const isServiceInstalled = computed<boolean>(
  () => !!serviceStatus.value.exe_path,
);
const canConfigure = computed<boolean>(
  () => isServiceInstalled.value || serviceModelsLoading.value,
);
function openConfigModal(): void {
  // Hard guard: the two service-config entrypoints (topbar ⚙️ and the
  // status-card gear) are disabled when the service is not installed, and
  // never open the config modal in that case (the runtime
  // `service_config.json` lives next to the installed binary, so it cannot be
  // edited before install). The install / models paths are set in the page's
  // paths section, not in this modal.
  if (!canConfigure.value) return;
  configModalOpen.value = true;
}
async function closeConfigModal(): Promise<void> {
  configModalOpen.value = false;
  // The modal may have edited service_launch — re-sync our launch params.
  await svc.loadServiceStatus();
  await svc.loadServiceModels();
}

async function onInstallDirSaved(): Promise<void> {
  await svc.loadServiceStatus();
  await svc.loadServiceModels();
}

const toast = useToastStore();

async function onSaveModelConfig(value: { modelRoot: string; modelName: string }): Promise<void> {
  svcParams.value.models_root_path = value.modelRoot;
  await svc.saveServiceParams();
  if (!value.modelName) return;
  if (!serviceModels.value.some((m) => m.name === value.modelName)) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: t("service.modelNotInList", { name: value.modelName }),
      timeoutMs: 5000,
    });
    return;
  }
  selectedServiceModelName.value = value.modelName;
  svc.saveServiceModelPreference(value.modelName);
}

// ESC closes the Service Config modal (V2 per-overlay self-managed ESC,
// mirroring ConfirmDialog.vue:53-83 — not a global keymap, not V1's monolithic
// app.js global handler). `useEscClose`'s `when` predicate makes the listener
// a no-op while the overlay is closed.
useEscClose(
  (ev) => {
    ev.preventDefault();
    void closeConfigModal();
  },
  () => configModalOpen.value,
);

// --- Navigation helpers (mirror V1 goTo*) ---
function goToDownloadService(): void {
  void router.push({ name: "downloads", query: { tab: "service" } });
}
function goToDownloadModels(): void {
  void router.push({ name: "downloads", query: { tab: "models" } });
}
// The GenieAPIService directory and model config file are set in
// ServicePathsSection (empty = defaults `data/bin/` / `data/models/`). The
// guidance links route the user to the Download Center, which installs
// under those defaults.

// V1 (index.html:3003) anchors the fullscreen log overlay's left edge to the
// sidebar width, switching between collapsed (60px) and expanded (260px) so
// the overlay stays flush with the sidebar in both states.
const sidebarOffset = computed(() => (ui.sidebarCollapsed ? "60px" : "260px"));

// V1 topbar "Refresh Status": re-fetch health + status + models on demand.
async function refreshStatus(): Promise<void> {
  void store.fetchHealth();
  await svc.loadServiceStatus();
  await svc.loadServiceModels();
}

// Under <KeepAlive> (AppMain.vue) ServiceView is cached, not unmounted, on
// navigate-away. Drive the live log-stream + status-polling lifecycle with
// activate/deactivate so the stream pauses while the view is hidden and
// resumes on return. We ALSO keep onMounted/onBeforeUnmount as a safety net
// for non-KeepAlive contexts (component-level unit tests / future routing
// changes). `svc.init()` is re-entrant + epoch-guarded so a duplicated
// invocation on first mount (onMounted + onActivated both fire) is benign:
// the second init bumps the epoch, the first's post-await `streamLogs()` /
// `startStatusPolling()` is skipped, only the latest init wins.
// `svc.dispose()` is idempotent (stop a null timer + abort a null controller
// are both no-ops), so the duplicated teardown on real unmount is harmless.
function activateService(): void {
  void store.fetchHealth();
  void svc.init();
}

onMounted(activateService);
onActivated(activateService);
onDeactivated(() => {
  svc.dispose();
});
onBeforeUnmount(() => {
  svc.dispose();
});

// ─── Topbar actions (V1 parity: index.html — service page header
// hosts ⚙️ Config / 🔄 Refresh / 📄 Copy Logs / 🗑 Clear Logs) ─────────
// Reboot is NOT exposed on the service panel (matching V1 — see header
// notice line 19-20); the topbar mirrors V1's flat action group.
// Copy/Clear are disabled when the log buffer is empty (V1 parity).
const noLogs = computed(() => serviceLogs.value.length === 0);
useHeaderActions(() => [
  {
    id: "service.config",
    label: t("service.config"),
    iconSvg: ICON_SETTINGS,
    title: canConfigure.value
      ? t("service.config")
      : t("service.configRequiresInstall"),
    disabled: !canConfigure.value,
    onClick: () => {
      openConfigModal();
    },
  },
  {
    id: "service.refresh",
    label: t("service.refreshStatus"),
    iconSvg: ICON_REFRESH,
    title: t("service.refreshStatus"),
    onClick: () => {
      void refreshStatus();
    },
  },
  {
    id: "service.copyLogs",
    label: t("service.copyLogs"),
    iconSvg: ICON_DOCUMENT,
    title: t("service.copyLogs"),
    disabled: noLogs.value,
    onClick: () => {
      void svc.copyLogs();
    },
  },
  {
    id: "service.clearLogs",
    label: t("service.clearLogs"),
    iconSvg: ICON_TRASH,
    title: t("service.clearLogs"),
    onClick: () => {
      void svc.clearLogs();
    },
  },
]);
</script>

<template>
  <section
    class="panel-view service-view"
    :aria-label="t('nav.service')"
  >
    <!-- ── 1. Connection bar (collapsible) ─────────────────────────────── -->
    <ServiceConnectionBar
      v-model:collapsed="connectionCollapsed"
      v-model:local-port="svcParams.local_port"
      :local-url="localUrl"
      :connection-testing="connectionTesting"
      :connection-test-result="connectionTestResult"
      :svc-params-saving="svcParamsSaving"
      @test="svc.testConnection"
      @save="svc.saveServiceParams"
    />

    <ServicePathsSection
      :models-root-path="svcParams.models_root_path"
      :selected-model-config-path="serviceModels.find((m) => m.name === selectedServiceModelName)?.config_path ?? ''"
      :exe-path="serviceStatus.exe_path ?? ''"
      @install-dir-saved="onInstallDirSaved"
      @save-model-config="onSaveModelConfig"
    />

    <!-- ── 2. Path-safety warning banner ───────────────────────────────── -->
    <ServicePathWarning
      v-if="serviceStatus.path_warning && !isRunning"
      :path-warning="serviceStatus.path_warning"
      @redownload="goToDownloadService"
    />

    <!-- ── 3. Status card + 4. not-installed guidance card ─────────────── -->
    <ServiceStatusCard
      :service-status="serviceStatus"
      :is-running="isRunning"
      :service-starting="serviceStarting"
      :service-stopping="serviceStopping"
      :service-clearing="serviceClearing"
      :service-models-loading="serviceModelsLoading"
      :service-models-count="serviceModels.length"
      :can-start-service="canStartService"
      :can-configure="canConfigure"
      :uptime-text="formatUptime(serviceStatus.uptime_seconds)"
      @open-config="openConfigModal"
      @start="svc.startServiceAction"
      @stop="svc.stopServiceAction"
      @clear="svc.clearServiceAction"
      @download-service="goToDownloadService"
      @download-models="goToDownloadModels"
    />

    <!-- ── 5. Launch Parameters (collapsible) ──────────────────────────── -->
    <ServiceLaunchParams
      v-model:collapsed="paramsCollapsed"
      v-model:selected-model="selectedServiceModelName"
      v-model:local-port="svcParams.local_port"
      v-model:log-level="svcParams.loglevel"
      :svc-params-saving="svcParamsSaving"
      :service-models="serviceModels"
      :service-models-loading="serviceModelsLoading"
      :service-models-by-accel="serviceModelsByAccel"
      :is-any-model-installing="isAnyModelInstalling"
      :is-any-model-downloading="isAnyModelDownloading"
      :exe-path="serviceStatus.exe_path"
      :service-command-preview="serviceCommandPreview"
      @save="svc.saveServiceParams"
      @model-change="svc.saveServiceModelPreference"
      @download-models="goToDownloadModels"
    />

    <!-- ── 6. Logs (extracted to ServiceLogPanel; state stays in the
         page-level useServiceControl, ref透传 via defineExpose) ── -->
    <ServiceLogPanel
      ref="logPanelComp"
      v-model:expanded="logsExpanded"
      :logs="serviceLogs"
      :streaming="serviceLogsStreaming"
      :user-scrolled-up="logUserScrolledUp"
      :sidebar-offset="sidebarOffset"
      @scroll="svc.onLogScroll"
      @scroll-top="svc.scrollLogToTop()"
      @scroll-bottom="svc.scrollLogToBottom(true, true)"
    />

    <!-- Service Config Modal -->
    <ServiceConfigModal
      v-model:open="configModalOpen"
      @close="closeConfigModal"
    />
  </section>
</template>

<style scoped>
.service-view {
  display: flex;
  flex-direction: column;
  /* V1 service.css:14 — gap: var(--space-4) */
  gap: var(--space-4);
}
</style>