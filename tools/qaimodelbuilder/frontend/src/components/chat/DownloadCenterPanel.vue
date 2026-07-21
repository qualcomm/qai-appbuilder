<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  DownloadCenterPanel — top-level Download Center panel.

  V1-parity layout (`DownloadCenterPanel.js:81-1210`):
    1. panel header: title + subtitle (V1 nav.downloads + service/models hint)
    2. ui-tabs: "GenieAPIService" / "Local Models"
    3. Aria2cBanner (5-state)
    4. DownloadSettingsPanel (collapsed by default; auto-expanded by the
       save_dir-unsafe warning link)
    5. ActiveDownloadsBar (only when ≥1 task is in flight)
    6. Tab content via `v-show` (preserves DOM/state on tab switch)

  Provides `useDownloadCenter` orchestrator via `provideDownloadCenter` so
  every nested card / banner / dialog can `injectDownloadCtx` without
  prop-drilling. The orchestrator is the singleton from
  `useDownloadsStore` so an in-flight SSE stream survives panel re-mounts
  (e.g. when the user navigates between `/downloads` and `/updates`).

  V1 reference: DownloadCenterPanel.js:81-235 + 1170-1210.
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { useRoute } from "vue-router";

import { useDownloadsStore } from "@/stores/downloads";
import { provideDownloadCenter } from "@/composables/useDownloadCenter";
import { useHeaderActions } from "@/composables/useHeaderActions";
import { ICON_REFRESH, ICON_TRASH } from "@/components/icons/topbarIcons";
import type { DownloadCenterTab } from "@/types/downloads";

import Aria2cBanner from "./../downloads/Aria2cBanner.vue";
import ActiveDownloadsBar from "./../downloads/ActiveDownloadsBar.vue";
import DownloadSettingsPanel from "./../downloads/DownloadSettingsPanel.vue";
import ModelCatalogTab from "./../downloads/ModelCatalogTab.vue";
import ServiceVersionsTab from "./../downloads/ServiceVersionsTab.vue";
import UiTabs from "./UiTabs.vue";

const ctx = useDownloadsStore();
provideDownloadCenter(ctx);

const { t } = useI18n();
const route = useRoute();

// ─── Tab strip ────────────────────────────────────────────────────────────

interface UiTabItem {
  id: string;
  label: string;
  badge?: string | number;
  badgeVariant?: "default" | "downloading";
}

const tabs = computed<UiTabItem[]>(() => [
  {
    id: "service",
    // V1 (downloads.css:51-74) shows a pulsing orange "●" badge while a
    // service download is in flight. Use UiTabs' badge slot with the
    // ``downloading`` variant rather than concatenating into the label —
    // keeps the visual semantics (color + pulse) instead of a static dot.
    label: "⚙️ " + t("downloads.tabService"),
    badge: ctx.isAnyServiceDownloading.value ? "●" : undefined,
    badgeVariant: ctx.isAnyServiceDownloading.value ? "downloading" : "default",
  },
  {
    id: "models",
    label: "🧠 " + t("downloads.tabModels"),
    // While downloading: orange pulsing ● (V1 parity). Otherwise: model
    // count in the default accent-light pill. Falsy 0 renders nothing.
    badge: ctx.isAnyModelDownloading.value
      ? "●"
      : ctx.modelCatalog.modelCount.value,
    badgeVariant: ctx.isAnyModelDownloading.value ? "downloading" : "default",
  },
]);

const tabModel = computed<string>({
  get: () => ctx.activeTab.value,
  set: (v) => ctx.setActiveTab(v as DownloadCenterTab),
});

const subtitleKey = computed<string>(() =>
  ctx.activeTab.value === "models"
    ? "downloads.subtitleModels"
    : "downloads.subtitleService",
);

// ─── Header refresh (V1 panel header "🔄 Refresh" button — re-fetches the
//     active tab's manifest on demand; V1 empty-state hints point users
//     to this "Refresh button in the upper right") ───────────────────────

/** Whether the active tab's catalog/version fetch is in flight. */
const refreshing = computed<boolean>(() =>
  ctx.activeTab.value === "models"
    ? ctx.modelCatalog.loading.value
    : ctx.serviceVersions.loading.value,
);

function refreshActiveTab(): void {
  if (ctx.activeTab.value === "models") {
    void ctx.modelCatalog.fetchCatalog();
  } else {
    void ctx.serviceVersions.fetchVersions();
  }
}

// ─── Clear finished (V1 parity) ──────────────────────────────────────
//
// V1 (index.html:388) shows "🗑 Clear finished" whenever ANY entry is in
// a terminal state — `done`, `error`, or `cancelled`. The button removes
// all such entries from the per-task map; in-flight entries
// (`downloading` / `preparing` / `installing` …) are untouched. Clearing
// only `done` would strand failed/cancelled entries forever and forces
// a page reload — that's a regression vs V1.
const TERMINAL_STATES = ["done", "error", "cancelled"] as const;
type TerminalStatus = (typeof TERMINAL_STATES)[number];
function isTerminal(status: string): status is TerminalStatus {
  return (TERMINAL_STATES as readonly string[]).includes(status);
}
const hasCompleted = computed<boolean>(() =>
  Object.values(ctx.downloads.value).some((d) => isTerminal(d.status)),
);

function clearCompleted(): void {
  // Mutate by replacement (Vue 3 deep reactivity does follow this, but a
  // wholesale replacement is the clearest invalidation signal for any
  // computed depending on the map).
  const next: Record<string, typeof ctx.downloads.value[string]> = {};
  for (const [k, v] of Object.entries(ctx.downloads.value)) {
    if (!isTerminal(v.status)) next[k] = v;
  }
  ctx.downloads.value = next;
}

// ─── Topbar actions (V1 parity: index.html:382-391 — refresh comes
// FIRST, then clear-finished. The refresh button morphs its icon into
// a spinner while either tab is fetching, matching V1's
// `<span class="spinner">` swap.) ──────────────────────────────────
const REFRESH_SPINNER_SVG =
  '<span class="spinner" style="width:14px;height:14px;border-width:2px"></span>';

useHeaderActions(() => {
  const actions = [];
  // 1) Refresh — first, with V1-style spinner-while-loading icon swap.
  if (refreshing.value) {
    actions.push({
      id: "downloads.refresh",
      label: t("common.refresh"),
      iconSvg: REFRESH_SPINNER_SVG,
      title: t("downloads.checkUpdate"),
      disabled: true,
      onClick: refreshActiveTab,
    });
  } else {
    actions.push({
      id: "downloads.refresh",
      label: t("common.refresh"),
      iconSvg: ICON_REFRESH,
      title: t("downloads.checkUpdate"),
      disabled: false,
      onClick: refreshActiveTab,
    });
  }
  // 2) Clear finished — only when there is something terminal to clear.
  if (hasCompleted.value) {
    actions.push({
      id: "downloads.clearFinished",
      label: t("index.clearFinished"),
      iconSvg: ICON_TRASH,
      title: t("index.clearFinished"),
      onClick: clearCompleted,
    });
  }
  return actions;
});

// ─── Lifecycle ────────────────────────────────────────────────────────────

onMounted(() => {
  // V1 parity: `navigateTo('updates', null, 'service'|'models')` deep-links
  // straight to a sub-tab (app.js:1964-1968 sets `dcActiveTab`). Mirror it by
  // honoring `?tab=service|models` so jumps from the Service page (and any
  // future deep links) land on the right sub-tab, not just the panel root.
  const q = route.query.tab;
  if (q === "service" || q === "models") {
    ctx.setActiveTab(q);
  }
  void ctx.init();
});

onBeforeUnmount(() => {
  // Disposal is the responsibility of the singleton store (it persists
  // across route swaps between /downloads and /updates). We deliberately
  // do NOT dispose here so an in-flight SSE download survives navigation.
});
</script>

<template>
  <div class="panel-view downloads-view">
    <!-- Header (title + subtitle only; the refresh button is registered
         as a topbar action via useHeaderActions for V1 parity — V1 hosts
         page-action buttons on the global topbar, not inside the body
         panel-header). -->
    <header class="downloads-view__header">
      <div class="downloads-view__heading">
        <h1 class="downloads-view__title">
          <svg
            class="downloads-view__title-icon"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <line
              x1="12"
              y1="3"
              x2="12"
              y2="15"
            />
            <polyline points="6 9 12 15 18 9" />
            <line
              x1="5"
              y1="21"
              x2="19"
              y2="21"
            />
          </svg>
          {{ t("downloads.title") }}
        </h1>
        <p class="downloads-view__subtitle">
          {{ t(subtitleKey) }}
        </p>
      </div>
    </header>

    <UiTabs
      v-model="tabModel"
      :tabs="tabs"
      variant="underline"
      :label="t('downloads.title')"
      class="downloads-view__tabs"
    />

    <!-- aria2c 5-state banner -->
    <Aria2cBanner
      :status="ctx.aria2c.status.value"
      :banner="ctx.aria2c.banner.value"
    />

    <!-- Download Settings (collapsible) -->
    <DownloadSettingsPanel
      :settings="ctx.settings.settings.value"
      :saving="ctx.settings.saving.value"
      @update:settings="(s) => ctx.settings.patch(s)"
      @save="ctx.saveDownloadSettings()"
    />

    <!-- Active downloads summary (only when ≥1 task is in flight) -->
    <ActiveDownloadsBar :active="ctx.activeDownloads.value" />

    <!-- Tabs preserved with v-show to keep per-tab state on switch -->
    <ServiceVersionsTab v-show="ctx.activeTab.value === 'service'" />
    <ModelCatalogTab v-show="ctx.activeTab.value === 'models'" />
  </div>
</template>

<style scoped>
/*
  V1 parity (downloads.css:10-15): `.download-center` is full-width with no
  max-width / margin:auto. The outer page padding (32px) is provided by the
  shared `.panel-view` container (layout.css:553 → --space-8 = 32px on wide
  screens, --space-6 = 24px default). We therefore DON'T re-declare padding
  here (that would override the panel-view value and re-center the panel) —
  we only own the inner flex/gap of the download center.
*/
.downloads-view {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  width: 100%;
}

.downloads-view__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.downloads-view__heading {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.downloads-view__title {
  margin: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-lg);
  font-weight: 700;
}

/* V1 panel-title-icon: 22px accent-tinted line-art download glyph */
.downloads-view__title-icon {
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  color: var(--accent);
  filter: drop-shadow(0 0 4px rgba(94, 234, 212, 0.18));
}

.downloads-view__subtitle {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
}
</style>
