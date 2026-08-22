<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  ModelCard — V1 "Local Models" tab model card (one `CatalogModel`).

  Renders one hardware-grouped catalog model with:
    - header (type icon 🖼/🧠 + name + ★recommended + hardware-dependent
      tag sequence + family · parameter_size subtitle)
    - context line (when context_length > 0)
    - description (short_description || description)
    - collapsible feature/detail block (card-local state)
    - variant region (single-variant / multi-variant selector / no-variant
      legacy compat — three branches, render-identical to V1)
    - DownloadProgress detail block while a task is non-`idle`
    - 6-state action row (installed / idle / preparing / downloading / done /
      error|cancelled), delete confirms via the project-wide `useConfirm()`
      (NEVER native confirm)

  V2 design notes (better than V1):
    - V1 copy-pasted the entire ~130-line template THREE times (single /
      multi / legacy) per hardware group (× NPU/GPU/CPU = 9 copies). Here the
      shared progress + action row is expressed ONCE, driven by computed
      `taskId` / `downloadStatus` / `localStatus`, with only the variant
      header differing per branch.
    - V1 hard-coded warning colours (#fff3cd / #ffc107 / #7c5c00). Here we use
      project CSS tokens (--banner-warn-bg / --banner-warn-border / --warning).

  V1 reference: DownloadCenterPanel.js:529-751 (NPU) / 761-774 (GPU header) /
  989-999 (CPU header). The three groups are structurally identical; only the
  header tag sequence differs (driven by `model.hardware`).
-->
<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import type {
  CatalogModel,
  DownloadStateEntry,
  DownloadStatus,
  LocalItemStatus,
  ModelVariant,
} from "@/types/downloads";
import { formatBytes, hasUnsafePath } from "@/composables/downloads/format";
import { findVariant } from "@/composables/downloads/useModelCatalog";
import { useLocalize } from "@/composables/useLocalize";
import DownloadProgress from "./DownloadProgress.vue";
import ModelCardActions from "./ModelCardActions.vue";
import PlatformSegmented from "./PlatformSegmented.vue";

interface Props {
  model: CatalogModel;
  /** Currently selected variant_id (multi-variant only; undefined → default). */
  selectedVariantId: string | undefined;
  /** Resolved task id for the (model, selected variant) tuple (`taskIdFor(m)`). */
  taskId: string;
  /** Per-task download state (`null` when no download has been started). */
  downloadEntry: DownloadStateEntry | null;
  /** Local-disk derivation row (`null` when not on disk). */
  localStatus: LocalItemStatus | null;
  /** Whether ANY download is in flight (used to disable Start). */
  isAnyDownloading: boolean;
  /** Map of variant_id → status, used to colour the segmented status dots. */
  variantStatuses: Record<string, DownloadStatus | undefined>;
  /**
   * Post-download install (unzip-to-models) lifecycle state (V1
   * `getInstallModelStatus`): `installing` while the install POST is in
   * flight (done row shows a spinner), `error` when it failed (done row keeps
   * a red message + delete-bad-file). `null` when idle/installed.
   */
  installState?: "installing" | "error" | null;
  /** Install failure message (shown as red text when `installState==='error'`). */
  installError?: string;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  selectVariant: [variantId: string];
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
const { localize, localizeAll } = useLocalize();

// Localized projections (re-evaluate on language switch — useI18n.locale is
// reactive). Catalog text comes as LocalizedText (string OR {lang: string})
// from the remote manifest; localize() picks the active UI language with the
// shared fallback chain.
const localizedDescription = computed(() => localize(props.model.description));
const localizedShortDescription = computed(() =>
  localize(props.model.short_description),
);
const localizedFeatures = computed(() => localizeAll(props.model.features));

// ─── Detail collapse (card-local UI state, not emitted) ────────────────────

const detailExpanded = ref(false);

const hasDetail = computed<boolean>(
  () =>
    localizedFeatures.value.length > 0 ||
    (localizedDescription.value !== "" &&
      localizedShortDescription.value !== ""),
);

// ─── Header derivation ─────────────────────────────────────────────────────

const isVlm = computed<boolean>(() => props.model.type === "vlm");

const isRecommended = computed<boolean>(() =>
  props.model.tags.includes("recommended"),
);

const isStable = computed<boolean>(() => props.model.tags.includes("stable"));

/** Tags shown as generic pills (excluding recommended / stable). */
const extraTags = computed<string[]>(() =>
  props.model.tags.filter((tag) => tag !== "recommended" && tag !== "stable"),
);

/**
 * Hardware-dependent tag pill sequence (V1 DownloadCenterPanel.js:536-541 NPU /
 * 768-771 GPU / 996-999 CPU). Data-driven here so the three render-identical
 * branches collapse into a single `v-for` (V2 better-than-V1: no template
 * duplication). Each pill carries an optional CSS modifier suffix.
 */
interface TagPill {
  label: string;
  /** `dc-card__tag--<mod>` modifier suffix (empty → plain pill). */
  mod: string;
}

const tagPills = computed<TagPill[]>(() => {
  const m = props.model;
  const pills: TagPill[] = [];
  // Hardware base tags.
  if (m.hardware === "npu") {
    pills.push({ label: "NPU", mod: "npu" }, { label: "QNN", mod: "qnn" });
  } else if (m.hardware === "gpu") {
    pills.push({ label: "GPU", mod: "gpu" }, { label: "GGUF", mod: "gguf" });
  } else {
    pills.push({ label: "CPU", mod: "cpu" });
    pills.push(
      m.format === "mnn"
        ? { label: "MNN", mod: "mnn" }
        : { label: "GGUF", mod: "gguf" },
    );
  }
  // VLM (vlm models only).
  if (isVlm.value) pills.push({ label: "VLM", mod: "vlm" });
  // Quantization.
  if (m.quantization) pills.push({ label: m.quantization, mod: "" });
  // Generic extra tags (NPU only in V1; harmless elsewhere as the list is empty).
  if (m.hardware === "npu") {
    for (const tag of extraTags.value) pills.push({ label: tag, mod: "" });
    if (isStable.value) pills.push({ label: "stable", mod: "stable" });
  }
  return pills;
});

const subtitle = computed<string>(
  () => `${props.model.family} · ${props.model.parameter_size}`,
);

const contextKTokens = computed<string>(() =>
  (props.model.context_length / 1024).toFixed(0),
);

const descriptionText = computed<string>(
  () => localizedShortDescription.value || localizedDescription.value,
);

const cardClass = computed<string>(() => {
  const classes = ["dc-card"];
  if (isRecommended.value) classes.push("dc-card-recommended");
  // V1 parity (downloads.css:165-178): the whole card switches border + faint
  // background tint by download status (downloading→amber / done→green /
  // error→red). Driven by the active download status so the card visually
  // tracks progress exactly like V1's `.dc-card.downloading/.done/.error`.
  const s = downloadStatus.value;
  if (s === "downloading" || s === "preparing") classes.push("downloading");
  else if (s === "done") classes.push("done");
  else if (s === "error" || s === "cancelled") classes.push("error");
  return classes.join(" ");
});

// ─── Variant region ────────────────────────────────────────────────────────

// Last path segment of a file path or URL (drops trailing slashes). Used to
// match an on-disk artifact's file name against a variant's download_url so
// the right platform tab is highlighted.
function basenameOf(p: string): string {
  const cleaned = p.replace(/[\\/]+$/, "");
  if (!cleaned) return "";
  const segs = cleaned.split(/[\\/]/);
  return segs[segs.length - 1] ?? "";
}

const variants = computed<ModelVariant[]>(() => props.model.variants);
const isMultiVariant = computed<boolean>(() => variants.value.length > 1);
const isSingleVariant = computed<boolean>(() => variants.value.length === 1);

/** The single variant (single-variant branch only). */
const singleVariant = computed<ModelVariant | null>(() =>
  isSingleVariant.value ? variants.value[0]! : null,
);

/** The currently-selected variant (multi-variant branch). */
const selectedVariant = computed<ModelVariant | null>(() =>
  findVariant(props.model, props.selectedVariantId),
);

const platformOptions = computed(() =>
  variants.value.map((v) => ({
    id: v.variant_id,
    label: v.platform,
    status: props.variantStatuses[v.variant_id] ?? null,
  })),
);

/** Composed hint line for the selected variant (V1 line 635). */
const platformHint = computed<string>(() => {
  const v = selectedVariant.value;
  if (v === null) return "";
  const parts: string[] = [];
  if (v.chip) parts.push(v.chip);
  const vdesc = localize(v.description);
  if (vdesc) parts.push(vdesc);
  const tail = parts.join(" · ");
  const sizePart = v.size_bytes > 0 ? formatBytes(v.size_bytes) : "";
  return [tail, sizePart].filter((p) => p !== "").join(" · ");
});

/** The variant driving the Start-button label (multi → selected platform). */
const startVariantPlatform = computed<string>(
  () => selectedVariant.value?.platform ?? "",
);

// ─── Download / install state machine (shared by all three branches) ───────

const downloadStatus = computed<DownloadStatus | null>(
  () => props.downloadEntry?.status ?? null,
);

const installPath = computed<string>(
  () => props.localStatus?.install_path ?? "",
);

const installPathUnsafe = computed<boolean>(() =>
  hasUnsafePath(installPath.value),
);

const isInstalled = computed<boolean>(
  () => props.localStatus?.installed === true,
);

/**
 * The variant id whose artifact is on disk (drives the platform label on the
 * Installed pill). Resolution order, mirroring ServiceVersionCard:
 *
 *   1. ``localStatus.platform_driver`` — the install marker
 *      (``.qai-install.json``) the backend wrote at install time. Stores the
 *      ``variant_id`` directly, so a match is exact and survives the zip
 *      being deleted post-install.
 *   2. The downloaded zip's file name vs each variant's ``download_url``
 *      basename (best-effort fallback for a downloaded-but-not-installed
 *      state, where the marker doesn't exist yet).
 *   3. Single-variant model — when the model exposes exactly one platform
 *      variant there is no ambiguity, so an installed-but-marker-less copy
 *      (a legacy install written before the marker existed, whose zip was
 *      deleted post-install leaving no on-disk tag) still resolves to that
 *      sole variant's platform. A multi-variant legacy install genuinely has
 *      no on-disk signal left, so it stays correctly degraded (no suffix).
 *
 * Returns "" when nothing matches — the pill then renders without a platform
 * suffix (acceptable degraded state, since the install path itself is still
 * shared and consistent across tabs).
 */
const installedVariantId = computed<string>(() => {
  const status = props.localStatus;
  if (status === null) return "";
  // (1) Install marker / config-derived variant — authoritative when present.
  //     ``platform_driver`` is either the exact ``variant_id`` (written by the
  //     install marker) OR the variant DIR segment recovered from the model's
  //     config.json internal paths (``models/<seg>/...``). The catalog
  //     variant_id may carry an extra suffix the dir segment omits (e.g.
  //     catalog ``qwen3-8b-8480-qnn2.44`` vs config segment ``qwen3-8b-8480``),
  //     so we accept an exact match first, then a prefix match either way.
  const markerVariant = status.platform_driver ?? "";
  if (markerVariant) {
    const direct = props.model.variants.find(
      (v) => v.variant_id === markerVariant,
    );
    if (direct) return direct.variant_id;
    const prefixed = props.model.variants.find(
      (v) =>
        v.variant_id.startsWith(markerVariant) ||
        markerVariant.startsWith(v.variant_id),
    );
    if (prefixed) return prefixed.variant_id;
  }
  // (2) Downloaded-zip file name fallback.
  const artifact = status.save_path || status.install_path;
  if (artifact) {
    const base = basenameOf(artifact).toLowerCase();
    if (base) {
      const match = props.model.variants.find((v) => {
        const urlBase = basenameOf(v.download_url).toLowerCase();
        return urlBase !== "" && urlBase === base;
      });
      if (match) return match.variant_id;
    }
  }
  // (3) Single-variant fallback — an installed model with exactly one
  // platform variant is unambiguous, so a legacy marker-less install (zip
  // deleted, no tag on disk) still gets its platform label. Gated on
  // `installed` so a not-yet-installed single-variant model isn't labelled.
  if (status.installed === true && props.model.variants.length === 1) {
    return props.model.variants[0]!.variant_id;
  }
  return "";
});

/** Platform label for the resolved installed variant (e.g. "Snapdragon X2 Elite"). */
const installedPlatformLabel = computed<string>(() => {
  const vid = installedVariantId.value;
  if (!vid) return "";
  const v = props.model.variants.find((vv) => vv.variant_id === vid);
  return v?.platform ?? "";
});

/**
 * When the user installs a variant, switch the active platform tab to match.
 * The install dir → variant_id mapping makes this deterministic regardless of
 * which variant was the default. Without this, installing the X2 Elite (8480)
 * variant left the card pinned on the X Elite (8380) tab — the user couldn't
 * see "this is the variant I just installed" and the Installed pill rendered
 * on the wrong tab.
 *
 * Only emits when the resolved id is known AND differs from the current
 * selection (avoids an emit loop with the parent v-model).
 */
watch(
  [isInstalled, installedVariantId],
  ([installed, vid]) => {
    if (installed && vid && vid !== props.selectedVariantId) {
      emit("selectVariant", vid);
    }
  },
  { immediate: true },
);

/**
 * Disk state: model zip downloaded but not yet installed (V1
 * useDownloadCenter.js:489-510). Lets the card recover the install/delete
 * row after a page reload drops the in-memory download entry.
 */
const isDownloaded = computed<boolean>(
  () => props.localStatus?.downloaded === true,
);

const downloadedSavePath = computed<string>(
  () => props.localStatus?.save_path ?? "",
);

const showProgress = computed<boolean>(
  () =>
    props.downloadEntry !== null &&
    downloadStatus.value !== null &&
    downloadStatus.value !== "idle",
);

/** Start-button label: single/legacy → "Download"; multi → "Download {platform}". */
const startLabel = computed<string>(() => {
  if (isMultiVariant.value) {
    return t("downloads.downloadPlatform", { platform: startVariantPlatform.value });
  }
  return t("downloads.startDownload");
});

// ─── Helpers ───────────────────────────────────────────────────────────────

function onToggleDetail(): void {
  detailExpanded.value = !detailExpanded.value;
}
</script>

<template>
  <article
    :class="cardClass"
    :data-model="model.model_id"
  >
    <!-- ── header ─────────────────────────────────────────────────────── -->
    <header class="dc-card__header">
      <div class="dc-card__header-top">
        <span
          class="dc-card__icon"
          :class="{ 'dc-card__icon--vlm': isVlm }"
          aria-hidden="true"
        >{{ isVlm ? "🖼️" : "🧠" }}</span>
        <div class="dc-card__header-meta">
          <h3 class="dc-card__title">
            <span>{{ model.name }}</span>
            <span
              v-if="isRecommended"
              class="dc-card__star"
              :title="t('downloads.recommended')"
              aria-hidden="true"
            >★</span>

            <!-- hardware-dependent tag pills (data-driven, single v-for) -->
            <span
              v-for="(pill, i) in tagPills"
              :key="`${pill.label}-${i}`"
              class="dc-card__tag"
              :class="pill.mod ? `dc-card__tag--${pill.mod}` : ''"
            >{{ pill.label }}</span>
          </h3>
          <p class="dc-card__subtitle">
            {{ subtitle }}
          </p>
        </div>
      </div>
    </header>

    <!-- ── context line ──────────────────────────────────────────────── -->
    <p
      v-if="model.context_length > 0"
      class="dc-card__context"
    >
      <span class="dc-card__context-label">{{ t("downloads.context") }}</span>
      <span>{{ contextKTokens }}K tokens</span>
    </p>

    <!-- ── description ───────────────────────────────────────────────── -->
    <p
      v-if="descriptionText"
      class="dc-card__desc"
    >
      {{ descriptionText }}
    </p>

    <!-- ── feature / detail collapse ─────────────────────────────────── -->
    <div
      v-if="hasDetail"
      class="dc-card__detail"
    >
      <button
        type="button"
        class="dc-card__detail-toggle"
        :aria-expanded="detailExpanded"
        @click="onToggleDetail"
      >
        <span
          class="dc-card__detail-caret"
          :class="{ 'dc-card__detail-caret--open': detailExpanded }"
          aria-hidden="true"
        >▶</span>
        <span>{{
          detailExpanded
            ? t("downloads.collapseDetail")
            : t("downloads.viewDetail")
        }}</span>
      </button>
      <div
        v-if="detailExpanded"
        class="dc-card__detail-body"
      >
        <ul
          v-if="localizedFeatures.length > 0"
          class="dc-card__feature-list"
        >
          <li
            v-for="f in localizedFeatures"
            :key="f"
          >
            {{ f }}
          </li>
        </ul>
        <p
          v-if="localizedDescription && localizedShortDescription"
          class="dc-card__detail-desc"
        >
          {{ localizedDescription }}
        </p>
      </div>
    </div>

    <!-- ── single-variant info row ───────────────────────────────────── -->
    <p
      v-if="isSingleVariant && singleVariant"
      class="dc-card__variant-line"
    >
      <span class="dc-card__variant-platform">{{ singleVariant.platform }}</span>
      <span
        v-if="singleVariant.chip"
        class="dc-card__variant-chip"
      >{{
        singleVariant.chip
      }}</span>
      <span
        v-if="singleVariant.min_driver_version"
        class="dc-card__variant-driver"
      >🔧 {{ singleVariant.min_driver_version }}</span>
    </p>

    <!-- ── multi-variant selector + hint ─────────────────────────────── -->
    <div
      v-if="isMultiVariant"
      class="dc-card__platforms"
    >
      <PlatformSegmented
        :options="platformOptions"
        :model-value="selectedVariantId ?? platformOptions[0]?.id ?? ''"
        :aria-label="t('downloads.downloadPlatform', { platform: '' })"
        @update:model-value="(id) => emit('selectVariant', id)"
      />
      <p
        v-if="selectedVariant"
        class="dc-card__platform-hint"
      >
        <!-- V1 parity: em-dash is a SEPARATOR between driver-req and the
             chip line, not a leading prefix. When `min_driver_version` is
             absent (most NPU variants), the chip line starts directly
             with the platform info — exactly as V1 (DownloadCenterPanel.js
             line ~635 conditionally prepends "🔧 … — " only when driver
             metadata exists). The previous `<span> — {{ hint }}</span>`
             form leaked the em-dash even when no driver was shown. -->
        <template v-if="selectedVariant.min_driver_version">
          <span>
            🔧 {{ t("downloads.requiresDriver") }}
            <code>{{ selectedVariant.min_driver_version }}</code>
          </span>
          <span v-if="platformHint"> — {{ platformHint }}</span>
        </template>
        <span v-else-if="platformHint">{{ platformHint }}</span>
      </p>
    </div>

    <!-- ── progress (only while non-idle) ────────────────────────────── -->
    <DownloadProgress
      v-if="showProgress && downloadEntry"
      :entry="downloadEntry"
    />

    <!-- ── 6-state action row (extracted child) ──────────────────────── -->
    <ModelCardActions
      :model-name="model.name"
      :download-entry="downloadEntry"
      :is-installed="isInstalled"
      :installed-platform-label="installedPlatformLabel"
      :install-path="installPath"
      :install-path-unsafe="installPathUnsafe"
      :is-any-downloading="isAnyDownloading"
      :start-label="startLabel"
      :downloaded="isDownloaded"
      :downloaded-save-path="downloadedSavePath"
      :install-state="installState ?? null"
      :install-error="installError ?? ''"
      @start-download="emit('startDownload')"
      @cancel="emit('cancel')"
      @install-to-models="(sp: string) => emit('installToModels', sp)"
      @delete-model="emit('deleteModel')"
      @delete-downloaded="emit('deleteDownloaded')"
      @retry="emit('retry')"
      @clear-status="emit('clearStatus')"
    />
  </article>
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
  background: linear-gradient(90deg, var(--warning), var(--accent));
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

.dc-card__header-top {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
}

.dc-card__icon {
  width: 36px;
  height: 36px;
  border-radius: var(--radius-sm);
  background: var(--bg-tertiary);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--text-lg);
  line-height: 1.2;
  flex-shrink: 0;
}

/* VLM model icon gets a pink tinted block (V1 .dc-card-icon-vlm). */
.dc-card__icon--vlm {
  background: rgba(233, 30, 99, 0.1);
}

.dc-card__header-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
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

.dc-card__subtitle {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-secondary);
}

/* ── tags: hardware/format modifiers (V1 downloads.css:231-250, 576-578) ──
   V1 colour map: NPU/QNN→accent(purple) · GPU/GGUF→info(blue) ·
   CPU/MNN→success(green) · VLM→pink(#e91e63) · stable→green.
   V1 shape: matches global `.dc-tag` — `border-radius: var(--radius-xs)`,
   `padding: 1px 7px`. (Previously used 999px pill which drifted from V1
   and from the sibling `ServiceVersionCard.vue:540` definition.) */
.dc-card__tag {
  display: inline-block;
  padding: 1px 7px;
  border-radius: var(--radius-xs);
  font-size: var(--text-xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  background: var(--bg-tertiary);
  color: var(--text-muted);
}

.dc-card__tag--npu {
  background: rgba(108, 99, 255, 0.15);
  color: var(--accent);
}

.dc-card__tag--qnn {
  background: rgba(108, 99, 255, 0.12);
  color: var(--accent);
}

.dc-card__tag--gpu {
  background: rgba(33, 150, 243, 0.15);
  color: var(--info);
}

.dc-card__tag--gguf {
  background: rgba(33, 150, 243, 0.12);
  color: var(--info);
}

.dc-card__tag--cpu {
  background: rgba(76, 175, 80, 0.15);
  color: var(--success);
}

.dc-card__tag--mnn {
  background: rgba(76, 175, 80, 0.12);
  color: var(--success);
}

.dc-card__tag--vlm {
  background: rgba(233, 30, 99, 0.15);
  color: #e91e63;
}

.dc-card__tag--stable {
  background: rgba(76, 175, 80, 0.12);
  color: var(--success);
}

.dc-card__context {
  margin: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.dc-card__context-label {
  font-weight: 600;
}

.dc-card__desc {
  margin: 0;
  font-size: var(--text-sm);
  line-height: 1.45;
}

/* ── detail collapse ───────────────────────────────────────────────────── */
.dc-card__detail-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: transparent;
  border: none;
  padding: 0;
  cursor: pointer;
  font-size: var(--text-sm);
  color: var(--accent);
}

.dc-card__detail-caret {
  display: inline-block;
  transition: transform 0.2s ease;
}

.dc-card__detail-caret--open {
  transform: rotate(90deg);
}

.dc-card__detail-body {
  margin-top: 6px;
  font-size: var(--text-sm);
}

.dc-card__feature-list {
  margin: 0;
  padding-left: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.dc-card__feature-list li {
  display: flex;
  align-items: flex-start;
  gap: 6px;
}

.dc-card__feature-list li::before {
  content: "◆";
  color: var(--accent);
  font-size: 8px;
  margin-top: 5px;
  flex-shrink: 0;
}

.dc-card__detail-desc {
  margin: 6px 0 0;
  color: var(--text-muted);
  line-height: 1.45;
}

/* ── variant rows ──────────────────────────────────────────────────────── */
.dc-card__variant-line {
  margin: 0;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.dc-card__variant-platform {
  font-weight: 600;
  color: var(--text-primary);
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

.dc-card__platform-hint code {
  font-family: var(--font-mono);
}
</style>
