<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  ServiceVersionCard — V1 GenieAPIService release card.

  Renders one `ServiceVersion` with:
    - title (`GenieAPIService v{version}` + ★ when recommended + tags)
    - description / release_date / size / requires-driver line
    - changelog collapsible (when non-empty)
    - multi-package platform selector (when >1 packages)
    - 6-state action row (idle / preparing / downloading / done / error /
      cancelled, plus the special `installed` row when the version is
      already on disk)
    - DownloadProgress detail block while non-`idle`
    - delete confirm via the project-wide `useConfirm()` (NEVER native confirm)

  V1 reference: DownloadCenterPanel.js:286-474.
-->
<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import type {
  DownloadStateEntry,
  LocalItemStatus,
  ServicePackage,
  ServiceVersion,
} from "@/types/downloads";
import { formatBytes, hasUnsafePath } from "@/composables/downloads/format";
import { findPackage } from "@/composables/downloads/useServiceVersions";
import { isInstalledServiceRunning } from "@/api/downloads";
import { useConfirm } from "@/composables/useConfirm";
import { useLocalize } from "@/composables/useLocalize";
import { useToastStore } from "@/stores/toast";
import DownloadProgress from "./DownloadProgress.vue";
import PlatformSegmented from "./PlatformSegmented.vue";

interface Props {
  version: ServiceVersion;
  /** The currently selected platform_id for this version (undefined → default). */
  selectedPlatformId: string | undefined;
  /** Resolved task id (single → version, multi → version-platformId). */
  taskId: string;
  /** Per-task download state (`undefined` if no download has been started). */
  downloadEntry: DownloadStateEntry | null;
  /** Local-disk derivation row for this version (`null` if not on disk). */
  localStatus: LocalItemStatus | null;
  /** Whether ANY service download is in flight (used to disable Start). */
  isAnyDownloading: boolean;
  /**
   * aria2c status for the "Installing aria2c…" preparing-arm hint. The
   * card only checks `install_status === "installing"`.
   */
  aria2cInstalling: boolean;
  /**
   * Whether the unzip-to-bin install POST is currently in flight (V1
   * `installStatuses[v].status === 'installing'`). Shows a spinner row.
   */
  installing?: boolean;
  /**
   * The last install error for this version, if any (V1
   * `installStatuses[v].error`). Rendered as a red line under the install
   * row so the user can retry.
   */
  installError?: string;
  /** Map of platform_id → status, used to colour the segmented status dots. */
  platformStatuses: Record<string, DownloadStateEntry["status"] | undefined>;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  selectPlatform: [platformId: string];
  startDownload: [];
  cancel: [];
  installToBin: [savePath: string];
  deleteDownloaded: [];
  deleteInstalled: [stopRunning: boolean];
  retry: [];
  clearStatus: [];
}>();

const { t } = useI18n();
const { confirm } = useConfirm();
const { localize } = useLocalize();

// Localized projections (re-evaluate on language switch). The release manifest
// and per-package descriptions/changelog come as LocalizedText (string OR
// {lang:string}) from the remote source; localize() picks the active UI lang.
const localizedDescription = computed(() => localize(props.version.description));
const localizedChangelog = computed(() => localize(props.version.changelog));
const toast = useToastStore();

// V1 (DownloadCenterPanel.js:322-331) renders the changelog as a clickable
// row with a rotating ▶ arrow + a .dc-changelog body capped at 120px with a
// scrollbar. Each card owns its own toggle state — V1 used a parent-level
// `changelogExpanded[version]` map but per-card local state is cleaner and
// produces the identical user-perceived behaviour.
const changelogOpen = ref(false);
function toggleChangelog(): void {
  changelogOpen.value = !changelogOpen.value;
}

// ─── Derived view state ────────────────────────────────────────────────────

const selectedPackage = computed<ServicePackage | null>(() =>
  findPackage(props.version, props.selectedPlatformId),
);

/** Localized description of the currently-selected package (platform). */
const selectedPackageDescription = computed<string>(() =>
  selectedPackage.value ? localize(selectedPackage.value.description) : "",
);

const hasMultiplePackages = computed<boolean>(() => props.version.packages.length > 1);

const platformOptions = computed(() =>
  props.version.packages.map((p) => ({
    id: p.platform_id,
    label: p.platform,
    status: props.platformStatuses[p.platform_id] ?? null,
  })),
);

const isInstalled = computed<boolean>(
  () => props.localStatus?.installed === true,
);

// Extract the trailing driver tag (e.g. "v81") from an artifact filename /
// URL, mirroring the backend ``_driver_tag_of``. Used to attribute the
// installed/downloaded artifact to the correct platform package.
function driverTagOf(name: string): string {
  const stem = name.toLowerCase().endsWith(".zip") ? name.slice(0, -4) : name;
  const matches = [...stem.matchAll(/_v(\d+)(?![.\d])/g)];
  const last = matches.at(-1);
  return last ? `v${last[1]}` : "";
}

// The platform_id whose package matches the installed/downloaded driver tag
// (so the correct tab is highlighted + the install row names the platform).
const installedPlatformId = computed<string>(() => {
  const tag = props.localStatus?.platform_driver ?? "";
  if (!tag) return "";
  const match = props.version.packages.find(
    (p) => driverTagOf(p.download_url) === tag,
  );
  return match?.platform_id ?? "";
});

const installedPlatformLabel = computed<string>(() => {
  const id = installedPlatformId.value;
  const pkg = props.version.packages.find((p) => p.platform_id === id);
  return pkg?.platform ?? "";
});

// When this version is installed, make the matching platform tab the active
// one so the user sees the install attributed to the right platform (V2
// enhancement: the install/download state is per-platform on disk).
watch(
  [isInstalled, installedPlatformId],
  ([installed, platformId]) => {
    if (installed && platformId && platformId !== props.selectedPlatformId) {
      emit("selectPlatform", platformId);
    }
  },
  { immediate: true },
);

const downloadStatus = computed<DownloadStateEntry["status"] | null>(
  () => props.downloadEntry?.status ?? null,
);

const isDownloadActive = computed<boolean>(() => {
  const s = downloadStatus.value;
  return s === "preparing" || s === "downloading";
});

const isDone = computed<boolean>(() => downloadStatus.value === "done");
const isErrorOrCancelled = computed<boolean>(() => {
  const s = downloadStatus.value;
  return s === "error" || s === "cancelled";
});

/**
 * "Downloaded but not installed" disk state (V1
 * useDownloadCenter.js:427-450). After a download completes, the
 * in-memory `downloadEntry` is lost on page reload / re-entry, but the
 * zip still sits on disk (`localStatus.downloaded === true`). V1 rebuilt
 * a synthetic `done` download entry from the disk scan so the card kept
 * showing "Install to bin / Delete". V2 keeps `localStatus` as the
 * authoritative disk source and derives this branch directly — when the
 * version is downloaded, not yet installed, and has no live download
 * entry, surface the same install/delete row. `save_path` comes from the
 * disk scan rather than the (absent) download entry.
 */
const isDownloadedNotInstalled = computed<boolean>(
  () =>
    props.localStatus?.downloaded === true &&
    props.localStatus?.installed !== true &&
    props.downloadEntry === null,
);

const downloadedSavePath = computed<string>(
  () => props.localStatus?.save_path ?? "",
);

const installPath = computed<string>(
  () => props.localStatus?.install_path ?? "",
);

const installPathUnsafe = computed<boolean>(() =>
  hasUnsafePath(installPath.value),
);

const cardClass = computed<string>(() => {
  const classes = ["dc-card"];
  if (props.version.is_recommended) classes.push("dc-card-recommended");
  // V1 parity (downloads.css:165-178): whole-card border + tint by status.
  if (isDownloadActive.value) classes.push("downloading");
  else if (isDone.value) classes.push("done");
  else if (isErrorOrCancelled.value) classes.push("error");
  return classes.join(" ");
});

// Tags excluding 'stable' / 'recommended' (rendered separately).
const visibleTags = computed<string[]>(() =>
  props.version.tags.filter(
    (t) => t !== "stable" && t !== "recommended" && t !== "Recommended",
  ),
);

// Whether the version has the 'stable' tag (rendered with dedicated class).
const isStable = computed<boolean>(() => props.version.tags.includes("stable"));

// ─── Actions ───────────────────────────────────────────────────────────────

async function copy(text: string): Promise<void> {
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

async function onInstall(): Promise<void> {
  // Prefer the live download entry's save_path; fall back to the disk
  // scan path for the "downloaded but not installed" reload case.
  const sp = props.downloadEntry?.save_path ?? downloadedSavePath.value;
  if (sp === undefined || sp === "") return;
  emit("installToBin", sp);
}

async function onDeleteInstalled(): Promise<void> {
  // Real-state probe: if the GenieAPIService for this version is still
  // running, deleting its files would fail on Windows (loaded Genie.dll is
  // locked → WinError 5). Warn the user it will be stopped first, then pass
  // stopRunning so the backend stops it gracefully before deleting.
  let running = false;
  try {
    const r = await isInstalledServiceRunning(props.version.version);
    running = r.running;
  } catch {
    // Probe failure is non-fatal — fall back to the plain delete confirm.
    running = false;
  }
  const message = running
    ? t("downloads.confirmDeleteVersionRunningMsg", {
        version: props.version.version,
      })
    : t("downloads.confirmDeleteVersionMsg", {
        version: props.version.version,
      });
  const ok = await confirm({
    icon: "🗑",
    title: t("downloads.confirmDeleteTitle"),
    message,
    confirmText: t("downloads.confirmDeleteBtn"),
    cancelText: t("downloads.cancelBtn"),
    confirmStyle: "danger",
  });
  if (ok) emit("deleteInstalled", running);
}

async function onDeleteDownloaded(): Promise<void> {
  const ok = await confirm({
    icon: "🗑",
    title: t("downloads.deleteDownloadTitle"),
    message: t("downloads.deleteServiceMsg", {
      version: props.version.version,
    }),
    confirmText: t("downloads.confirmDeleteBtn"),
    cancelText: t("downloads.cancelBtn"),
    confirmStyle: "danger",
  });
  if (ok) emit("deleteDownloaded");
}
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- v-html renders only our own static, trusted i18n catalog strings (no user/remote input); not an XSS vector. -->
  <article
    :class="cardClass"
    :data-version="version.version"
  >
    <header class="dc-card__header">
      <div
        class="dc-card__icon"
        aria-hidden="true"
      >
        ⚙️
      </div>
      <div class="dc-card__head-meta">
        <h3 class="dc-card__title">
          <span>GenieAPIService v{{ version.version }}</span>
          <span
            v-if="version.is_recommended"
            class="dc-card__star"
            :title="t('downloads.recommended')"
            aria-hidden="true"
          >★</span>
          <span
            v-if="isStable"
            class="dc-card__tag dc-card__tag--stable"
          >stable</span>
          <span
            v-for="tag in visibleTags"
            :key="tag"
            class="dc-card__tag"
          >{{ tag }}</span>
        </h3>
        <p
          v-if="localizedDescription"
          class="dc-card__desc"
        >
          {{ localizedDescription }}
        </p>
        <!-- V1 parity (DownloadCenterPanel.js:302): when a version has no
             description, fall back to showing the download_url as the
             subtitle (ellipsised, full value in title). Without this the
             subtitle line was blank for description-less versions. -->
        <p
          v-else-if="version.download_url"
          class="dc-card__desc dc-card__desc--url"
          :title="version.download_url"
        >
          {{ version.download_url }}
        </p>
      </div>
    </header>
    <div class="dc-card__meta">
      <span v-if="version.release_date">
        <span class="dc-card__meta-label">{{ t("downloads.releaseDate") }}</span>
        {{ version.release_date }}
      </span>
      <span v-if="version.size_bytes > 0">
        <span class="dc-card__meta-label">{{ t("downloads.size") }}</span>
        {{ formatBytes(version.size_bytes) }}
      </span>
      <span v-if="version.min_driver_version && !hasMultiplePackages">
        🔧 {{ t("downloads.requiresDriver") }} {{ version.min_driver_version }}
      </span>
    </div>

    <!-- changelog (V1 toggle row + rotating ▶ + 120px max-height scroll body;
         DownloadCenterPanel.js:322-331 + downloads.css:449-462) -->
    <div
      v-if="localizedChangelog"
      class="dc-card__changelog-wrap"
    >
      <div
        class="dc-card__changelog-toggle"
        role="button"
        tabindex="0"
        :aria-expanded="changelogOpen"
        @click="toggleChangelog"
        @keydown.enter.prevent="toggleChangelog"
        @keydown.space.prevent="toggleChangelog"
      >
        <span
          class="dc-card__changelog-arrow"
          :class="{ 'is-open': changelogOpen }"
        >▶</span>
        {{ t("downloads.changelog") }}
      </div>
      <div
        v-if="changelogOpen"
        class="dc-changelog"
      >
        {{ localizedChangelog }}
      </div>
    </div>

    <!-- multi-package selector -->
    <div
      v-if="hasMultiplePackages"
      class="dc-card__platforms"
    >
      <PlatformSegmented
        :options="platformOptions"
        :model-value="selectedPlatformId ?? platformOptions[0]?.id ?? ''"
        :aria-label="t('downloads.downloadPlatform', { platform: '' })"
        @update:model-value="(id) => emit('selectPlatform', id)"
      />
      <p
        v-if="selectedPackage"
        class="dc-card__platform-hint"
      >
        <!-- V1 parity: em-dash is a SEPARATOR between driver-req and the
             description, not a leading prefix. When `min_driver_version`
             is absent, the description starts directly without the em-dash
             — matches V1 DownloadCenterPanel.js:352. The previous form
             always prepended " — " even with no driver line, breaking the
             user-perceived hint layout. -->
        <template v-if="selectedPackage.min_driver_version">
          <span>
            🔧 {{ t("downloads.requiresDriver") }}
            <code>{{ selectedPackage.min_driver_version }}</code>
          </span>
          <span v-if="selectedPackageDescription">
            — {{ selectedPackageDescription }}
          </span>
        </template>
        <span v-else-if="selectedPackageDescription">
          {{ selectedPackageDescription }}
        </span>
      </p>
    </div>

    <!-- progress (only while a download is non-idle) -->
    <DownloadProgress
      v-if="downloadEntry && downloadStatus && downloadStatus !== 'idle'"
      :entry="downloadEntry"
    />

    <!-- 0) installing → spinner (V1 installStatuses[v]==='installing',
         DownloadCenterPanel.js:460-465) — takes priority over install rows. -->
    <div
      v-if="installing"
      class="dc-card__row"
    >
      <span class="dc-card__hint">
        <span
          class="dc-card__spinner"
          aria-hidden="true"
        ></span>
        {{ t("downloads.installing") }}
      </span>
    </div>

    <!-- 1) installed → installed row + delete -->
    <div
      v-else-if="isInstalled && !isDownloadActive && !isDone"
      class="dc-card__row"
    >
      <span class="dc-card__installed-pill">
        ✓ {{ t("downloads.installed")
        }}<template v-if="installedPlatformLabel"> · {{ installedPlatformLabel }}</template>
      </span>
      <code class="dc-card__path">{{ installPath }}</code>
      <!-- V1 parity (DownloadCenterPanel.js:409-411): inline ⚠️ next to the
           install path when the path is unsafe, with the full hint tooltip.
           Mirrors ModelCardActions.vue's installed row (same .dc-card__warn-icon
           chrome) so service-version and model cards behave identically. -->
      <span
        v-if="installPathUnsafe"
        class="dc-card__warn-icon"
        :title="t('downloads.unsafePathFullHint')"
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
        @click="onDeleteInstalled"
      >
        {{ t("downloads.deleteVersion") }}
      </button>
    </div>
    <!-- V1 parity (DownloadCenterPanel.js:429): installPathUnsafe locale value
         contains <br> and <code> HTML — must use v-html to render correctly. -->
    <p
      v-if="isInstalled && installPathUnsafe"
      class="dc-card__warn"
      role="alert"
      v-html="'⚠️ ' + t('downloads.installPathUnsafe')"
    />

    <!-- 1b) downloaded but not installed (reload case) → save_path + install + delete -->
    <template v-else-if="isDownloadedNotInstalled">
      <!-- V1 parity (DownloadCenterPanel.js:393-397): the 📁 save_path row stays
           visible after reload (V1 rebuilt a synthetic `done` downloadStatus from
           the disk scan, keeping this row). V2 sources save_path from the disk
           scan (`localStatus.save_path`) since the live downloadEntry is gone. -->
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
        >{{
          downloadedSavePath
        }}</span>
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
          :title="t('downloads.installToBinTitle')"
          @click="onInstall"
        >
          {{ t("downloads.installToBin") }}
        </button>
        <button
          type="button"
          class="btn btn-danger btn-sm"
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
        ⬇
        {{
          hasMultiplePackages && selectedPackage
            ? t("downloads.downloadPlatform", { platform: selectedPackage.platform })
            : t("downloads.startDownload")
        }}
      </button>
    </div>

    <!-- 3) preparing → "Installing aria2c..." -->
    <div
      v-else-if="downloadStatus === 'preparing' && aria2cInstalling"
      class="dc-card__row"
    >
      <span class="dc-card__hint">
        ⟳ {{ t("downloads.installingAria2c") }}
      </span>
    </div>

    <!-- 4) downloading → cancel -->
    <div
      v-else-if="isDownloadActive"
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

    <!-- 5) done → install + copy + delete + close -->
    <div
      v-else-if="isDone"
      class="dc-card__row"
    >
      <button
        type="button"
        class="btn btn-primary btn-sm"
        :title="t('downloads.installToBinTitle')"
        @click="onInstall"
      >
        {{ t("downloads.installToBin") }}
      </button>
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
        class="btn btn-danger btn-sm"
        @click="onDeleteDownloaded"
      >
        {{ t("downloads.deleteBtn") }}
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
    <!-- install error (V1 installStatuses[v].error,
         DownloadCenterPanel.js:449-459) — red line under the action row so
         the user can retry "Install to bin". -->
    <p
      v-if="installError && !installing"
      class="dc-card__install-error"
      role="alert"
    >
      ⚠️ {{ installError }}
    </p>
  </article>
  <!-- eslint-enable vue/no-v-html -->
</template>

<style scoped>
.dc-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-4) 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-secondary);
}

.dc-card-recommended::before {
  content: "";
  position: absolute;
  inset: 0 0 auto 0;
  height: 3px;
  background: linear-gradient(
    90deg,
    var(--warning),
    var(--accent)
  );
  border-radius: var(--radius) var(--radius) 0 0;
}

/* ── card status tint (V1 downloads.css:165-178) ───────────────────────── */
.dc-card.downloading {
  border-color: var(--warning);
  background: rgba(255, 152, 0, 0.03);
}

.dc-card.done {
  border-color: var(--success);
  background: rgba(76, 175, 80, 0.03);
}

.dc-card.error {
  border-color: var(--error);
  background: rgba(244, 67, 54, 0.03);
}

.dc-card__header {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
}

/* V1 dc-card-icon: 36×36 tertiary-bg rounded square holding the ⚙️ glyph */
.dc-card__icon {
  width: 36px;
  height: 36px;
  border-radius: var(--radius-sm);
  background: var(--bg-tertiary);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--text-lg);
  flex-shrink: 0;
}

.dc-card__head-meta {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.dc-card__title {
  margin: 0;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: var(--text-md);
  font-weight: 600;
}

.dc-card__star {
  color: var(--warning);
}

.dc-card__tag {
  display: inline-flex;
  align-items: center;
  padding: 1px 7px;
  border-radius: var(--radius-xs);
  font-size: var(--text-xs);
  background: var(--bg-tertiary);
  color: var(--text-muted);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

/* V1 dc-tag-stable: green tint for stable releases */
.dc-card__tag--stable {
  background: rgba(76, 175, 80, 0.12);
  color: var(--success);
}

.dc-card__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-4);
  font-size: var(--text-sm);
  color: var(--text-secondary);
}

.dc-card__meta-label {
  color: var(--text-muted);
}

.dc-card__desc {
  margin: 0;
  font-size: var(--text-sm);
  line-height: 1.45;
}

/* V1 dc-card-subtitle: muted, single-line ellipsis URL fallback */
.dc-card__desc--url {
  color: var(--text-muted);
  font-family: var(--font-mono);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.dc-card__changelog-wrap {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* V1 toggle row (DownloadCenterPanel.js:323-329): text-muted small label
   + rotating ▶ arrow, click toggles. Use a button-like role with keyboard
   support so it's accessible (V2 enhancement, behaviour identical). */
.dc-card__changelog-toggle {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: var(--text-sm);
  color: var(--text-muted);
  cursor: pointer;
  user-select: none;
}

.dc-card__changelog-arrow {
  display: inline-block;
  transition: transform 0.2s;
}

.dc-card__changelog-arrow.is-open {
  transform: rotate(90deg);
}

/* V1 .dc-changelog (downloads.css:449-462): bordered body, 120px max-height,
   scrollable, mono whitespace. */
.dc-changelog {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  line-height: 1.6;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--space-2) var(--space-3);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 120px;
  overflow-y: auto;
}

.dc-card__platforms {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.dc-card__platform-hint {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--text-muted);
}

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

/* V1 parity (DownloadCenterPanel.js:409-411): inline unsafe-path ⚠️ icon in
   the installed row. Same chrome as ModelCardActions.vue's installed row. */
.dc-card__warn-icon {
  color: var(--warning);
  cursor: help;
  flex-shrink: 0;
}

/* V1 parity (downloads.css:347-367 .dc-save-path): 📁 save_path footer shown in
   the "downloaded but not installed" reload state so the user can see where the
   zip landed and copy its path. */
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

/* Inline spinner for the "Installing…" row (V1 installStatuses spinner). */
.dc-card__spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  margin-right: 6px;
  vertical-align: -1px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: dc-card-spin 0.7s linear infinite;
}

@keyframes dc-card-spin {
  to {
    transform: rotate(360deg);
  }
}

/* Install failure line (V1 installStatuses[v].error red text). */
.dc-card__install-error {
  margin: 0;
  padding: 6px 10px;
  font-size: var(--text-xs);
  color: var(--error);
  background: var(--banner-error-bg, rgba(244, 67, 54, 0.08));
  border: 1px solid var(--banner-error-border, rgba(244, 67, 54, 0.3));
  border-radius: var(--radius-sm);
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
}
</style>
