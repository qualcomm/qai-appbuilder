<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ImageDiffViewer — Super-Resolution output comparison view (V1 parity).
 *
 * Three modes (top chip tabs):
 *   * slider       — two images overlaid, draggable divider
 *   * side-by-side — left original / right upscaled
 *   * single       — only the output image
 *
 * Features:
 *   * Scroll-wheel zoom (0.5x–3x) on container
 *   * Slider drag via pointer events (cross-browser)
 *   * Image load error display
 *   * Lightbox click (custom event dispatch)
 *
 * Uses global `.ab-imgdiff-*` classes from styles/app-builder/app-builder.css.
 *
 * V1 source: QAIModelBuilder_v1_pure/frontend/js/components/app-builder/outputs/ImageDiffViewer.js
 */
import { computed, ref, onBeforeUnmount } from "vue";
import { useI18n } from "vue-i18n";

// ── Types ────────────────────────────────────────────────────────────────────

type ViewMode = "slider" | "side-by-side" | "single";

type SizePair = [number, number] | null;

interface Props {
  beforeUrl: string;
  afterUrl: string;
  /**
   * Model tensor input/output sizes (the neural net's in/out tensor dims).
   * V1 `ImageDiffViewer.js:144-145` (`model_in_size` / `model_out_size`).
   */
  modelInSize?: SizePair;
  modelOutSize?: SizePair;
  /**
   * Source image input/output sizes (the user's uploaded image → stitched
   * result). V1 `ImageDiffViewer.js:142-143` (`in_size` / `out_size`).
   */
  sourceInSize?: SizePair;
  sourceOutSize?: SizePair;
  /** Upscale factor (V1 `data.scale`). */
  scale?: number | string | null;
  /** Whether the source was processed in tiles (V1 `data.tiled`). */
  tiled?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  modelInSize: null,
  modelOutSize: null,
  sourceInSize: null,
  sourceOutSize: null,
  scale: null,
  tiled: false,
});

const { t } = useI18n();

// ── Size info (V1 ImageDiffViewer.js:140-164 parity) ─────────────────────────
//
// Information hierarchy (user's mental model):
//   ▸ Model in→out   — neural-net tensor input/output dims (describes the model)
//   ▸ Source in→out  — the user's uploaded image / stitched output pixel dims
//   ▸ tiled badge    — when source > model_in, the runner tiles + stitches
//
// Legacy runners (no model_*_size) fall back to a single Source row + scale,
// matching V1's `legacy` flag behavior.
function fmtSize(sz: SizePair): string {
  return sz !== null ? `${sz[0]}×${sz[1]}` : "";
}

const sizeInfo = computed(() => {
  const model =
    props.modelInSize !== null && props.modelOutSize !== null
      ? `${fmtSize(props.modelInSize)} → ${fmtSize(props.modelOutSize)}`
      : "";
  const source =
    props.sourceInSize !== null && props.sourceOutSize !== null
      ? `${fmtSize(props.sourceInSize)} → ${fmtSize(props.sourceOutSize)}`
      : "";
  const scaleText =
    props.scale !== null && props.scale !== undefined && props.scale !== ""
      ? `×${props.scale}`
      : "";
  return {
    model, // model tensor in→out
    source, // source → stitched result
    scale: scaleText,
    // legacy runner (no model_*_size): show a single row
    legacy: !model && !!source,
  };
});

const isTiled = computed<boolean>(() => props.tiled === true);

// ── Mode ─────────────────────────────────────────────────────────────────────

const mode = ref<ViewMode>("slider");

function setMode(m: ViewMode): void {
  mode.value = m;
}

// ── Zoom ─────────────────────────────────────────────────────────────────────

const zoom = ref(1);

function onWheel(e: WheelEvent): void {
  e.preventDefault();
  const delta = e.deltaY > 0 ? 0.9 : 1.1;
  zoom.value = Math.max(0.5, Math.min(3, zoom.value * delta));
}

function resetZoom(): void {
  zoom.value = 1;
}

// ── Input availability ───────────────────────────────────────────────────────

const hasInput = computed<boolean>(() => !!props.beforeUrl);

// ── Load errors ──────────────────────────────────────────────────────────────

const outputErr = ref(false);
const inputErr = ref(false);

function onOutputLoadError(): void {
  outputErr.value = true;
}
function onInputLoadError(): void {
  inputErr.value = true;
}

// ── Slider drag (pointer events for cross-browser) ───────────────────────────

const sliderPct = ref(50);
let dragging = false;
let dragRect: DOMRect | null = null;

function onSliderPointerDown(e: PointerEvent): void {
  const container = (e.currentTarget as HTMLElement)?.closest(
    ".ab-imgdiff-slider-container",
  );
  if (!container) return;
  dragging = true;
  dragRect = container.getBoundingClientRect();
  document.addEventListener("pointermove", onPointerMove);
  document.addEventListener("pointerup", onPointerUp);
  e.preventDefault();
}

function onPointerMove(e: PointerEvent): void {
  if (!dragging || !dragRect) return;
  const x = e.clientX - dragRect.left;
  sliderPct.value = Math.max(0, Math.min(100, (x / dragRect.width) * 100));
}

function onPointerUp(): void {
  dragging = false;
  dragRect = null;
  document.removeEventListener("pointermove", onPointerMove);
  document.removeEventListener("pointerup", onPointerUp);
}

onBeforeUnmount(() => {
  document.removeEventListener("pointermove", onPointerMove);
  document.removeEventListener("pointerup", onPointerUp);
});

// ── Lightbox ─────────────────────────────────────────────────────────────────

function openInLightbox(url: string): void {
  if (!url) return;
  try {
    const ev = new CustomEvent("app-builder-lightbox", {
      detail: { src: url },
    });
    window.dispatchEvent(ev);
  } catch {
    /* noop */
  }
}
</script>

<template>
  <div
    class="ab-imgdiff"
    @wheel="onWheel"
  >
    <!-- Mode toggle toolbar -->
    <div class="ab-imgdiff-toolbar">
      <div
        class="ab-imgdiff-modes"
        role="tablist"
      >
        <button
          type="button"
          class="ab-chip"
          :class="{ active: mode === 'slider' }"
          :disabled="!hasInput"
          :title="t('appBuilder.diffSliderTip', 'Drag the divider to compare')"
          @click="setMode('slider')"
        >
          {{ t("appBuilder.diffSlider", "Slider") }}
        </button>
        <button
          type="button"
          class="ab-chip"
          :class="{ active: mode === 'side-by-side' }"
          :disabled="!hasInput"
          @click="setMode('side-by-side')"
        >
          {{ t("appBuilder.diffSideBySide", "Side-by-side") }}
        </button>
        <button
          type="button"
          class="ab-chip"
          :class="{ active: mode === 'single' }"
          @click="setMode('single')"
        >
          {{ t("appBuilder.diffSingle", "Single") }}
        </button>
      </div>
      <div class="ab-imgdiff-meta">
        <!-- Model tensor in→out (V1 ImageDiffViewer.js:205-210) -->
        <span
          v-if="sizeInfo.model"
          class="ab-imgdiff-size ab-imgdiff-size-model"
        >
          <span class="ab-imgdiff-size-label">{{ t("appBuilder.diffModel", "Model") }}</span>
          <span class="ab-imgdiff-size-val">{{ sizeInfo.model }}</span>
          <span
            v-if="sizeInfo.scale"
            class="ab-imgdiff-size-scale"
          >{{ sizeInfo.scale }}</span>
        </span>
        <!-- Source → stitched result (V1 ImageDiffViewer.js:211-220) -->
        <span
          v-if="sizeInfo.source && !sizeInfo.legacy"
          class="ab-imgdiff-size ab-imgdiff-size-source"
        >
          <span class="ab-imgdiff-size-label">{{ t("appBuilder.diffSource", "Source") }}</span>
          <span class="ab-imgdiff-size-val">{{ sizeInfo.source }}</span>
          <span
            v-if="isTiled"
            class="ab-imgdiff-tag"
            :title="t('appBuilder.tiledHint', 'Source larger than model input — processed in tiles and stitched.')"
          >{{ t("appBuilder.tiled", "tiled") }}</span>
        </span>
        <!-- Legacy runner (no model_*_size): single In/Out row (V1 :221-229) -->
        <span
          v-if="sizeInfo.legacy"
          class="ab-imgdiff-size"
        >
          <span class="ab-imgdiff-size-val">{{ sizeInfo.source }}</span>
          <span
            v-if="sizeInfo.scale"
            class="ab-imgdiff-size-scale"
          >{{ sizeInfo.scale }}</span>
          <span
            v-if="isTiled"
            class="ab-imgdiff-tag"
            :title="t('appBuilder.tiledHint', 'Source larger than model input — processed in tiles and stitched.')"
          >{{ t("appBuilder.tiled", "tiled") }}</span>
        </span>
        <button
          type="button"
          class="ab-iconbtn"
          :title="t('appBuilder.resetZoom', 'Reset zoom')"
          @click="resetZoom"
        >
          {{ Math.round(zoom * 100) }}%
        </button>
      </div>
    </div>

    <!-- Content stage -->
    <div
      class="ab-imgdiff-stage"
      :data-zoomed="zoom > 1.001 ? 'true' : 'false'"
      :style="{ transform: `scale(${zoom})` }"
    >
      <!-- Slider mode -->
      <div
        v-if="mode === 'slider' && hasInput"
        class="ab-imgdiff-slider-container"
      >
        <!-- Base = input (before) fills container -->
        <img
          :src="beforeUrl"
          class="ab-imgdiff-slider-base"
          :alt="t('appBuilder.original')"
          @error="onInputLoadError"
        />
        <!-- Top = output (after), clipped from left to sliderPct -->
        <img
          :src="afterUrl"
          class="ab-imgdiff-slider-top"
          :alt="t('appBuilder.upscaled')"
          :style="{ clipPath: `inset(0 ${100 - sliderPct}% 0 0)` }"
          @error="onOutputLoadError"
        />
        <!-- Corner labels -->
        <span class="ab-imgdiff-slider-tag ab-imgdiff-slider-tag-left">
          {{ t("appBuilder.upscaled", "Upscaled") }}
        </span>
        <span class="ab-imgdiff-slider-tag ab-imgdiff-slider-tag-right">
          {{ t("appBuilder.original", "Original") }}
        </span>
        <!-- Draggable handle -->
        <div
          class="ab-imgdiff-slider-handle"
          :style="{ left: sliderPct + '%' }"
          role="separator"
          :aria-label="t('appBuilder.aria.comparisonDivider')"
          :title="t('appBuilder.diffSliderTip', 'Drag the divider to compare')"
          @pointerdown="onSliderPointerDown"
        >
          <div class="ab-imgdiff-slider-line"></div>
          <div
            class="ab-imgdiff-slider-knob"
            aria-hidden="true"
          >
            <span class="ab-imgdiff-slider-knob-arrow ab-imgdiff-slider-knob-arrow-l">&#8249;</span>
            <span class="ab-imgdiff-slider-knob-arrow ab-imgdiff-slider-knob-arrow-r">&#8250;</span>
          </div>
        </div>
      </div>

      <!-- Side-by-side mode -->
      <div
        v-else-if="mode === 'side-by-side' && hasInput"
        class="ab-imgdiff-sbs"
      >
        <figure>
          <img
            :src="beforeUrl"
            :alt="t('appBuilder.original')"
            @error="onInputLoadError"
            @click="openInLightbox(beforeUrl)"
          />
          <figcaption>{{ t("appBuilder.original", "Original") }}</figcaption>
        </figure>
        <figure>
          <img
            :src="afterUrl"
            :alt="t('appBuilder.upscaled')"
            @error="onOutputLoadError"
            @click="openInLightbox(afterUrl)"
          />
          <figcaption>{{ t("appBuilder.upscaled", "Upscaled") }}</figcaption>
        </figure>
      </div>

      <!-- Single mode -->
      <div
        v-else
        class="ab-imgdiff-single"
      >
        <img
          :src="afterUrl"
          :alt="t('appBuilder.upscaled')"
          @error="onOutputLoadError"
          @click="openInLightbox(afterUrl)"
        />
      </div>

      <!-- Load error messages -->
      <div
        v-if="outputErr"
        class="ab-imgdiff-err"
      >
        {{ t("appBuilder.imageLoadError", "Failed to load output image") }}: {{ afterUrl }}
      </div>
      <div
        v-if="inputErr && hasInput"
        class="ab-imgdiff-err"
      >
        {{ t("appBuilder.imageLoadError", "Failed to load input image") }}: {{ beforeUrl }}
      </div>
    </div>
  </div>
</template>
