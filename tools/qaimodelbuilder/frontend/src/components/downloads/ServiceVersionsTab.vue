<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  ServiceVersionsTab — V1 "service" tab container.

  Lists `ServiceVersion[]` (sorted descending by version) as a vertical
  stack of `ServiceVersionCard`. Renders an empty-state hint with a link
  to the Download Settings region when the version_list_url is unset
  (HTTP 422 `service_release.catalog_unavailable`) — V1 parity (panel
  283-288).

  Owns the wiring between cards and `useServiceVersions` (start /
  install / cancel / delete / select platform). The download state map
  is shared via `injectDownloadCtx`.

  V1 reference: DownloadCenterPanel.js:240-475.
-->
<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";

import type { DownloadStateEntry, ServiceVersion } from "@/types/downloads";
import { injectDownloadCtx } from "@/composables/useDownloadCenter";
import { useToastStore } from "@/stores/toast";
import ServiceVersionCard from "./ServiceVersionCard.vue";

const ctx = injectDownloadCtx();
const { t } = useI18n();
const toast = useToastStore();

const sv = ctx.serviceVersions;

/**
 * For a given service version, derive the platform_id → status map used to
 * decorate the segmented selector dots. Tasks are keyed by `version` (single)
 * or `version-platform_id` (multi).
 */
function platformStatusesFor(
  v: ServiceVersion,
): Record<string, DownloadStateEntry["status"] | undefined> {
  if (v.packages.length <= 1) return {};
  const result: Record<string, DownloadStateEntry["status"] | undefined> = {};
  for (const pkg of v.packages) {
    const tid = `${v.version}-${pkg.platform_id}`;
    result[pkg.platform_id] = ctx.downloads.value[tid]?.status;
  }
  return result;
}

const aria2cInstalling = computed<boolean>(
  () => ctx.aria2c.status.value.install_status === "installing",
);

// ─── Card → composable bridge ─────────────────────────────────────────────

async function startDownload(v: ServiceVersion): Promise<void> {
  const taskId = sv.taskIdFor(v);
  // V1 toast when re-clicking start on an already-active task.
  if (ctx.downloads.value[taskId] !== undefined) {
    const status = ctx.downloads.value[taskId]!.status;
    if (status === "preparing" || status === "downloading") {
      toast.push({
        id: crypto.randomUUID(),
        kind: "info",
        message: t("downloads.alreadyDownloading"),
        timeoutMs: 3000,
      });
      return;
    }
  }
  await sv.startDownload(v);
}

async function installToBin(v: ServiceVersion, savePath: string): Promise<void> {
  const res = await sv.install(v, savePath);
  if (res.ok) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "success",
      message: t("downloads.serviceInstalledToast", { version: v.version }),
      timeoutMs: 4000,
    });
  } else if (res.error !== undefined) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: t("downloads.installFailed", { msg: res.error }),
      timeoutMs: 5000,
    });
  }
}

async function deleteInstalled(
  v: ServiceVersion,
  stopRunning = false,
): Promise<void> {
  const res = await sv.deleteInstalled(v.version, stopRunning);
  if (res.ok) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "success",
      message: t("downloads.serviceDeletedToast", { version: v.version }),
      timeoutMs: 3000,
    });
  } else if (res.error !== undefined) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: t("downloads.deleteFailed", { msg: res.error }),
      timeoutMs: 5000,
    });
  }
}

async function deleteDownloaded(v: ServiceVersion): Promise<void> {
  const res = await sv.deleteDownloaded(v.version);
  if (res.ok) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "success",
      message: t("downloads.deletedToast"),
      timeoutMs: 3000,
    });
  } else if (res.error !== undefined) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: t("downloads.deleteFailed", { msg: res.error }),
      timeoutMs: 5000,
    });
  }
}

function cancel(v: ServiceVersion): void {
  ctx.cancelDownload(sv.taskIdFor(v));
  toast.push({
    id: crypto.randomUUID(),
    kind: "info",
    message: t("downloads.cancelledToast"),
    timeoutMs: 2500,
  });
}

function clearStatus(v: ServiceVersion): void {
  sv.clearStatus(sv.taskIdFor(v));
}

async function retry(v: ServiceVersion): Promise<void> {
  // Drop the prior terminal entry then start a fresh download.
  sv.clearStatus(sv.taskIdFor(v));
  await sv.startDownload(v);
}
</script>

<template>
  <section
    class="dc-tab dc-tab--service"
    aria-labelledby="dc-service-heading"
  >
    <h2
      id="dc-service-heading"
      class="qai-sr-only"
    >
      {{ t("downloads.tabService") }}
    </h2>

    <!-- Top info banner (V1 DownloadCenterPanel.js:254-258 — always shown
         above the version list, mirroring the Models tab modelsDesc banner). -->
    <div
      class="dc-tab__desc"
      role="note"
    >
      <span
        class="dc-tab__desc-icon"
        aria-hidden="true"
      >ℹ️</span>
      <span>{{ t("downloads.serviceDesc") }}</span>
    </div>

    <!-- empty state: catalog_url unset (422 unconfigured) — V1 parity:
         render the primary "check update" button so users can retry once
         they've configured the version_list_url (DownloadCenterPanel.js:272-282). -->
    <div
      v-if="sv.loadError.value?.kind === 'unconfigured'"
      class="dc-tab__empty"
      role="status"
    >
      <div
        class="dc-tab__empty-icon"
        aria-hidden="true"
      >
        📦
      </div>
      <strong>{{ t("downloads.emptyVersions") }}</strong>
      <p>{{ t("downloads.emptyVersionsHint") }}</p>
      <button
        type="button"
        class="btn btn-primary btn-sm"
        @click="sv.fetchVersions()"
      >
        {{ t("downloads.checkUpdate") }}
      </button>
    </div>

    <!-- empty state: transport / parse error -->
    <div
      v-else-if="sv.loadError.value?.kind === 'transport'"
      class="dc-tab__empty dc-tab__empty--error"
      role="alert"
    >
      <div
        class="dc-tab__empty-icon"
        aria-hidden="true"
      >
        📦
      </div>
      <strong>{{ t("downloads.fetchVersionsFailed") }}</strong>
      <p>{{ sv.loadError.value.message }}</p>
      <button
        type="button"
        class="btn btn-primary btn-sm"
        @click="sv.fetchVersions()"
      >
        {{ t("downloads.checkUpdate") }}
      </button>
    </div>

    <!-- empty list (URL configured but manifest empty) — V1 parity:
         primary "check update" button (DownloadCenterPanel.js:272-282). -->
    <div
      v-else-if="!sv.loading.value && sv.sortedVersions.value.length === 0"
      class="dc-tab__empty"
      role="status"
    >
      <div
        class="dc-tab__empty-icon"
        aria-hidden="true"
      >
        📦
      </div>
      <strong>{{ t("downloads.emptyVersions") }}</strong>
      <p>{{ t("downloads.emptyVersionsHint") }}</p>
      <button
        type="button"
        class="btn btn-primary btn-sm"
        @click="sv.fetchVersions()"
      >
        {{ t("downloads.checkUpdate") }}
      </button>
    </div>

    <!-- loading -->
    <div
      v-else-if="sv.loading.value"
      class="dc-tab__loading"
      role="status"
    >
      <span
        class="dc-tab__spinner"
        aria-hidden="true"
      ></span>
      {{ t("downloads.refreshing") }}
    </div>

    <!-- card list -->
    <div
      v-else
      class="dc-tab__list"
    >
      <ServiceVersionCard
        v-for="v in sv.sortedVersions.value"
        :key="v.version"
        :version="v"
        :selected-platform-id="sv.selectedPlatform.value[v.version]"
        :task-id="sv.taskIdFor(v)"
        :download-entry="ctx.downloads.value[sv.taskIdFor(v)] ?? null"
        :local-status="sv.localStatus.value[v.version] ?? null"
        :is-any-downloading="ctx.isAnyServiceDownloading.value"
        :aria2c-installing="aria2cInstalling"
        :installing="sv.installState.value[v.version] === 'installing'"
        :install-error="sv.installState.value[v.version] === 'error' ? (sv.installError.value[v.version] ?? '') : ''"
        :platform-statuses="platformStatusesFor(v)"
        @select-platform="(id) => sv.setSelectedPlatform(v.version, id)"
        @start-download="startDownload(v)"
        @cancel="cancel(v)"
        @install-to-bin="(savePath) => installToBin(v, savePath)"
        @delete-downloaded="deleteDownloaded(v)"
        @delete-installed="(stopRunning) => deleteInstalled(v, stopRunning)"
        @retry="retry(v)"
        @clear-status="clearStatus(v)"
      />
    </div>
  </section>
</template>

<style scoped>
/* Shared `.dc-tab__*` banner/empty/loading/spinner styles (deduped with
   ModelCatalogTab). Tab-specific styles stay below. */
@import "./downloads-tab-shared.css";

.dc-tab {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.dc-tab__list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.qai-sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
</style>
