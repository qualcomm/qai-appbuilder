<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  ModelCardActions — the 6-state action row of a `ModelCard`.

  Extracted from `ModelCard.vue` (single-responsibility / cohesion: keeps the
  card under the soft line budget — AGENTS.md §3.6 advisory). Owns the V1
  status-machine action UI + the install / delete / copy side-effects
  (`useConfirm` danger confirms — NEVER native confirm).

  States (V1 DownloadCenterPanel.js:598-621):
    1. installed   → installed pill + path + copy + delete
    2. idle        → start download
    3. preparing   → "Installing aria2c…"
    4. downloading → cancel
    5. done        → install + copy + delete-downloaded + close
    6. error/cancelled → retry + close
-->
<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";

import type { DownloadStateEntry, DownloadStatus } from "@/types/downloads";
import { useConfirm } from "@/composables/useConfirm";
import { useToastStore } from "@/stores/toast";

interface Props {
  /** Model display name (delete-confirm message interpolation). */
  modelName: string;
  /** Per-task download state (`null` when no download has been started). */
  downloadEntry: DownloadStateEntry | null;
  /** Whether the model is installed on disk. */
  isInstalled: boolean;
  /**
   * Display name of the installed platform variant (e.g. "Snapdragon X2
   * Elite"). Drives the pill suffix "Installed · {platform}", mirroring
   * ServiceVersionCard. Empty string ⇒ no platform suffix (graceful
   * degradation: a downloaded-only or platform-unknown install still shows
   * the bare "✓ Installed" pill).
   */
  installedPlatformLabel?: string;
  /** Resolved install path (installed row). */
  installPath: string;
  /** Whether the install path is unsafe (non-ASCII / spaces). */
  installPathUnsafe: boolean;
  /** Whether ANY download is in flight (disables Start / Retry). */
  isAnyDownloading: boolean;
  /** Start-button label (single/legacy → "Download"; multi → "Download {platform}"). */
  startLabel: string;
  /**
   * Disk state: model zip is downloaded but not yet installed (V1
   * useDownloadCenter.js:489-510). After a page reload the in-memory
   * `downloadEntry` is gone, but the zip still sits on disk; this lets the
   * card keep showing "Install to models / Delete".
   */
  downloaded: boolean;
  /** Disk save_path for the downloaded-not-installed reload case. */
  downloadedSavePath: string;
  /**
   * Post-download install (unzip) lifecycle state (V1 `getInstallModelStatus`,
   * DownloadCenterPanel.js:609-619): `installing` shows a spinner in the done
   * row, `error` shows a red message + "delete bad file" button. `null` when
   * idle / installed cleanly.
   */
  installState?: "installing" | "error" | null;
  /** Install failure message (shown when `installState==='error'`). */
  installError?: string;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  startDownload: [];
  cancel: [];
  installToModels: [savePath: string];
  /** Installed → full delete (also done-state "delete bad file"). */
  deleteModel: [];
  /** Done but not installed → delete downloaded file only. */
  deleteDownloaded: [];
  retry: [];
  clearStatus: [];
}>();

const { t } = useI18n();
const { confirm } = useConfirm();
const toast = useToastStore();

const status = computed<DownloadStatus | null>(
  () => props.downloadEntry?.status ?? null,
);

const isDownloadActive = computed<boolean>(() => {
  const s = status.value;
  return s === "preparing" || s === "downloading";
});

const isPreparing = computed<boolean>(() => status.value === "preparing");
const isDownloading = computed<boolean>(() => status.value === "downloading");
const isDone = computed<boolean>(() => status.value === "done");
const isErrorOrCancelled = computed<boolean>(() => {
  const s = status.value;
  return s === "error" || s === "cancelled";
});

/** V1 `getInstallModelStatus().status === 'installing'` (DownloadCenterPanel.js:616). */
const isInstalling = computed<boolean>(() => props.installState === "installing");
/** V1 `getInstallModelStatus().status === 'error'` (DownloadCenterPanel.js:610-613). */
const hasInstallError = computed<boolean>(() => props.installState === "error");

/**
 * "Downloaded but not installed" disk state (V1
 * useDownloadCenter.js:489-510). Model zip is on disk, not yet unzipped to
 * `models/`, and there is no live download entry (reload case).
 */
const isDownloadedNotInstalled = computed<boolean>(
  () => props.downloaded && !props.isInstalled && props.downloadEntry === null,
);

async function copy(text: string): Promise<void> {
  if (text === "") return;
  try {
    await navigator.clipboard.writeText(text);
    toast.push({
      id: crypto.randomUUID(),
      kind: "success",
      message: t("downloads.pathCopiedSimpleToast"),
      timeoutMs: 2000,
    });
  } catch {
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: t("downloads.copyFailedToast"),
      timeoutMs: 3000,
    });
  }
}

function onInstall(): void {
  const sp = props.downloadEntry?.save_path ?? props.downloadedSavePath;
  if (sp === undefined || sp === "") return;
  emit("installToModels", sp);
}

async function onDeleteModel(): Promise<void> {
  const ok = await confirm({
    icon: "🗑",
    title: t("downloads.deleteModelTitle"),
    message: t("downloads.deleteModelMsg", { name: props.modelName }),
    confirmText: t("downloads.deleteBtn"),
    cancelText: t("downloads.cancelBtn"),
    confirmStyle: "danger",
  });
  if (ok) emit("deleteModel");
}

async function onDeleteDownloaded(): Promise<void> {
  const ok = await confirm({
    icon: "🗑",
    title: t("downloads.deleteDownloadedTitle"),
    message: t("downloads.deleteModelDownloadMsg", { name: props.modelName }),
    confirmText: t("downloads.deleteBtn"),
    cancelText: t("downloads.cancelBtn"),
    confirmStyle: "danger",
  });
  if (ok) emit("deleteDownloaded");
}
</script>

<template>
  <!-- 1) installed → installed pill + path + copy + delete -->
  <template v-if="isInstalled && !isDownloadActive && !isDone">
    <div class="dc-card__install-row">
      <span class="dc-card__installed-pill">
        ✓ {{ t("downloads.installed")
        }}<template v-if="props.installedPlatformLabel">
          · {{ props.installedPlatformLabel }}</template>
      </span>
      <code
        class="dc-card__path"
        :title="installPath"
      >{{ installPath }}</code>
      <span
        v-if="installPathUnsafe"
        class="dc-card__warn-icon"
        :title="t('downloads.unsafePathHint')"
      >⚠️</span>
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        :title="t('downloads.copyInstallPath')"
        @click="copy(installPath)"
      >
        📋 {{ t("downloads.copyPath") }}
      </button>
      <button
        type="button"
        class="btn btn-danger btn-sm"
        :title="t('downloads.deleteInstallTitle')"
        @click="onDeleteModel"
      >
        {{ t("downloads.deleteModel") }}
      </button>
    </div>
    <p
      v-if="installPathUnsafe"
      class="dc-card__warn"
      role="alert"
    >
      ⚠️ {{ t("downloads.unsafeModelPathWarn") }}
    </p>
  </template>

  <!-- 1b) downloaded but not installed (reload case) → save_path + install + delete -->
  <template v-else-if="isDownloadedNotInstalled">
    <!-- V1 parity (DownloadCenterPanel.js:587-591 / 661-665): 📁 save_path row
         stays visible after reload so the user can see where the model zip
         landed and copy its path. save_path comes from the disk scan. -->
    <div
      v-if="downloadedSavePath"
      class="dc-card__save-path"
    >
      <span
        class="dc-card__save-path-icon"
        aria-hidden="true"
      >📁</span>
      <span
        class="dc-card__save-path-text"
        :title="downloadedSavePath"
      >{{ downloadedSavePath }}</span>
      <button
        type="button"
        class="btn btn-icon dc-card__save-path-copy"
        :title="t('downloads.copyPath')"
        @click="copy(downloadedSavePath)"
      >
        ⧉
      </button>
    </div>
    <div class="dc-card__row">
      <button
        type="button"
        class="btn btn-primary btn-sm"
        :title="t('downloads.unzipToModels')"
        @click="onInstall"
      >
        {{ t("downloads.installToModels") }}
      </button>
      <button
        type="button"
        class="btn btn-danger btn-sm"
        :title="t('downloads.deleteDownloadedTitle')"
        @click="onDeleteDownloaded"
      >
        {{ t("downloads.deleteBtn") }}
      </button>
    </div>
  </template>

  <!-- 2) idle (no download yet, not installed) → start download -->
  <div
    v-else-if="!downloadEntry && !isInstalled"
    class="dc-card__row"
  >
    <button
      type="button"
      class="btn btn-primary btn-sm"
      :disabled="isAnyDownloading"
      @click="emit('startDownload')"
    >
      ⬇ {{ startLabel }}
    </button>
  </div>

  <!-- 3) preparing → "Installing aria2c..." -->
  <div
    v-else-if="isPreparing"
    class="dc-card__row"
  >
    <span class="dc-card__hint">⟳ {{ t("downloads.installingAria2c") }}</span>
  </div>

  <!-- 4) downloading → cancel -->
  <div
    v-else-if="isDownloading"
    class="dc-card__row"
  >
    <button
      type="button"
      class="btn btn-danger-outline btn-sm"
      @click="emit('cancel')"
    >
      {{ t("downloads.cancelBtn") }}
    </button>
  </div>

  <!-- 5) done → install + copy + delete-downloaded + close -->
  <div
    v-else-if="isDone"
    class="dc-card__row"
  >
    <!-- 5a) installing (unzip in flight) → spinner (V1 DownloadCenterPanel.js:616) -->
    <span
      v-if="isInstalling"
      class="dc-card__hint dc-card__hint--installing"
    >
      <span
        class="spinner dc-card__spinner"
        aria-hidden="true"
      ></span>
      {{ t("downloads.installing") }}
    </span>

    <!-- 5b) not yet installed OR install failed → Install (+ retry-able) -->
    <template v-else>
      <button
        type="button"
        class="btn btn-primary btn-sm"
        :title="t('downloads.unzipToModels')"
        @click="onInstall"
      >
        {{ t("downloads.installToModels") }}
      </button>
      <!-- install error red message (V1 DownloadCenterPanel.js:612) -->
      <span
        v-if="hasInstallError"
        class="dc-card__install-error"
        role="alert"
      >{{ installError }}</span>
      <!-- install error → "delete bad file" (V1 DownloadCenterPanel.js:613) -->
      <button
        v-if="hasInstallError"
        type="button"
        class="btn btn-danger btn-sm"
        :title="t('downloads.deleteBadFileTitle')"
        @click="onDeleteModel"
      >
        {{ t("downloads.deleteBadFile") }}
      </button>
      <!-- no install attempt yet → delete the downloaded zip (V1 line 614) -->
      <button
        v-else-if="!isInstalled"
        type="button"
        class="btn btn-danger btn-sm"
        :title="t('downloads.deleteDownloadedTitle')"
        @click="onDeleteDownloaded"
      >
        {{ t("downloads.deleteBtn") }}
      </button>
    </template>

    <button
      v-if="downloadEntry?.save_path"
      type="button"
      class="btn btn-ghost btn-sm"
      :title="t('downloads.copyPath')"
      @click="copy(downloadEntry.save_path)"
    >
      📋 {{ t("downloads.copyPath") }}
    </button>
    <button
      type="button"
      class="btn btn-ghost btn-sm"
      @click="emit('clearStatus')"
    >
      {{ t("downloads.closeBtn") }}
    </button>
  </div>

  <!-- 6) error / cancelled → retry + close -->
  <div
    v-else-if="isErrorOrCancelled"
    class="dc-card__row"
  >
    <button
      type="button"
      class="btn btn-primary btn-sm"
      :disabled="isAnyDownloading"
      @click="emit('retry')"
    >
      {{ t("downloads.retryBtn") }}
    </button>
    <button
      type="button"
      class="btn btn-ghost btn-sm"
      @click="emit('clearStatus')"
    >
      {{ t("downloads.closeBtn") }}
    </button>
  </div>
</template>

<style scoped>
.dc-card__row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.dc-card__installed-pill {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--success);
  background: rgba(45, 165, 86, 0.15);
  border: 1px solid var(--banner-success-border);
}

.dc-card__path {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  background: var(--bg-code);
  padding: 1px 6px;
  border-radius: 3px;
  word-break: break-all;
  flex: 1 1 auto;
  min-width: 0;
}

.dc-card__warn-icon {
  color: var(--warning);
  cursor: help;
}

/* V1 parity (downloads.css:347-367 .dc-save-path): 📁 save_path footer shown in
   the "downloaded but not installed" reload state. */
.dc-card__save-path {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-xs);
  color: var(--text-muted);
  font-family: var(--font-mono);
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 6px var(--space-3);
  margin-bottom: var(--space-2);
  overflow: hidden;
}

.dc-card__save-path-icon {
  color: var(--success);
  flex-shrink: 0;
}

.dc-card__save-path-text {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dc-card__save-path-copy {
  flex-shrink: 0;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 2px 5px;
  cursor: pointer;
}

.dc-card__hint {
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.dc-card__hint--installing {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.dc-card__spinner {
  width: 12px;
  height: 12px;
  border-width: 2px;
}

.dc-card__install-error {
  font-size: var(--text-xs);
  color: var(--error);
  word-break: break-word;
}

.dc-card__warn {
  margin: 0;
  padding: 6px 10px;
  font-size: var(--text-xs);
  color: var(--warning);
  background: var(--banner-warn-bg);
  border: 1px solid var(--banner-warn-border);
  border-radius: var(--radius-sm);
  line-height: 1.6;
}
</style>
