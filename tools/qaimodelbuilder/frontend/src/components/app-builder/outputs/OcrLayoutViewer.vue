<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * OcrLayoutViewer — OCR output visualization (PP-OCRv4, V1 `OcrLayoutViewer.js`
 * behavior parity).
 *
 * V2 reimplementation in TS/Composition API of V1's JS viewer. The runner
 * output schema (factory/chat_features/app-builder/models/ppocrv4/manifest.json
 * `outputSchema.jsonSchema`) is the field source of truth:
 *
 *   lines[].box      — quadrilateral [x1,y1,x2,y2,x3,y3,x4,y4] in PIXEL coords
 *                      (vertices TL→TR→BR→BL), NOT a normalized [x,y,w,h] rect.
 *   lines[].text     — recognized text
 *   lines[].conf     — recognition confidence in [0,1]
 *   lines[].line_idx — 0-based reading-order index
 *   fullText         — all line texts joined by '\n'
 *   lang_detected    — heuristic 'zh' | 'en' | 'mixed'
 *   page_size        — source image [width, height] in pixels
 *
 * Render (V1 OcrLayoutViewer.js:17-21):
 *   • Image mode (only when the source image is available): original image with
 *     a canvas overlay tracing each quadrilateral box; hovering a line in the
 *     list highlights its box; confidence heat color (≥0.8 green / 0.5–0.8
 *     orange / <0.5 red).
 *   • Text mode: full line list, each row [idx] text conf%.
 *
 * Uses the global `.ab-ocr-*` classes from styles/app-builder/app-builder.css
 * (shared with the V1-parity layout — no scoped styles here).
 */
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import { useI18n } from "vue-i18n";

/** OCR line — matches the runner `lines[]` item schema. */
export interface OcrLine {
  /** Quadrilateral [x1,y1,x2,y2,x3,y3,x4,y4] in pixel coords (TL→TR→BR→BL). */
  box: number[];
  text: string;
  /** Recognition confidence in [0,1]. */
  conf: number;
  /** 0-based reading-order index. */
  line_idx?: number;
}

/** Legacy block shape (normalized [x,y,w,h] bbox) — transitional only. */
interface LegacyOcrBlock {
  text: string;
  bbox: [number, number, number, number];
  confidence: number;
}

interface Props {
  /** Detected lines (reading order). */
  lines?: OcrLine[];
  /**
   * Legacy block list (normalized bbox) used by the older `AppBuilderView.vue`
   * consumer. Transitional shim until that view migrates to `lines`.
   */
  blocks?: LegacyOcrBlock[];
  /** Resolved URL of the source image (parent resolves run.inputs.image). */
  imageUrl?: string;
  /** Source image [width, height] in pixels (canvas overlay scaling). */
  pageSize?: [number, number] | null;
  /** Full recognized text (joined by '\n'). */
  fullText?: string;
  /** Heuristic language: 'zh' | 'en' | 'mixed'. */
  langDetected?: string;
}

const props = withDefaults(defineProps<Props>(), {
  lines: undefined,
  blocks: undefined,
  imageUrl: "",
  pageSize: null,
  fullText: "",
  langDetected: "",
});

const { t } = useI18n();

/**
 * Render lines. Prefers the canonical `lines` (8-point pixel quadrilateral +
 * conf); falls back to the legacy `blocks` (normalized [x,y,w,h]) by converting
 * each rect to a quadrilateral so the same canvas/list code path handles both.
 */
const renderLines = computed<OcrLine[]>(() => {
  if (Array.isArray(props.lines)) return props.lines;
  const blocks = props.blocks;
  if (!Array.isArray(blocks)) return [];
  return blocks.map((b, i) => {
    const x = b.bbox[0];
    const y = b.bbox[1];
    const w = b.bbox[2];
    const h = b.bbox[3];
    return {
      box: [x, y, x + w, y, x + w, y + h, x, y + h],
      text: b.text,
      conf: b.confidence,
      line_idx: i,
    };
  });
});

const hasImage = computed<boolean>(
  () => props.imageUrl !== "" && props.pageSize !== null,
);

// ── Canvas overlay ──────────────────────────────────────────────────────────
const imgEl = ref<HTMLImageElement | null>(null);
const canvasEl = ref<HTMLCanvasElement | null>(null);
const hoveredLineIdx = ref<number | null>(null);
const imgLoadErr = ref(false);

function confColor(conf: number): string {
  if (typeof conf !== "number") return "rgba(99,179,237,0.9)";
  if (conf >= 0.8) return "rgba(72,187,120,0.9)";
  if (conf >= 0.5) return "rgba(237,137,54,0.9)";
  return "rgba(245,101,101,0.9)";
}

function tracePath(
  ctx: CanvasRenderingContext2D,
  box: number[],
  sx: number,
  sy: number,
): void {
  ctx.beginPath();
  ctx.moveTo((box[0] ?? 0) * sx, (box[1] ?? 0) * sy);
  ctx.lineTo((box[2] ?? 0) * sx, (box[3] ?? 0) * sy);
  ctx.lineTo((box[4] ?? 0) * sx, (box[5] ?? 0) * sy);
  ctx.lineTo((box[6] ?? 0) * sx, (box[7] ?? 0) * sy);
  ctx.closePath();
}

function drawBoxes(): void {
  const cv = canvasEl.value;
  const im = imgEl.value;
  if (cv === null || im === null) return;
  const ps = props.pageSize;
  if (ps === null) return;

  const dispW = im.clientWidth;
  const dispH = im.clientHeight;
  if (dispW === 0 || dispH === 0) return;

  // canvas is absolutely positioned at the container padding-box top-left, while
  // the img sits inside the content-box. Compute the img's offset relative to
  // the canvas so the box coordinate system aligns with the displayed image.
  let offX = 0;
  let offY = 0;
  const offsetParent = cv.offsetParent;
  if (offsetParent !== null && im.offsetParent === offsetParent) {
    offX = im.offsetLeft - cv.offsetLeft;
    offY = im.offsetTop - cv.offsetTop;
  } else {
    const ir = im.getBoundingClientRect();
    const cr = cv.getBoundingClientRect();
    offX = ir.left - cr.left;
    offY = ir.top - cr.top;
  }

  const dpr = Math.max(1, window.devicePixelRatio || 1);
  cv.style.width = `${dispW}px`;
  cv.style.height = `${dispH}px`;
  cv.width = Math.floor(dispW * dpr);
  cv.height = Math.floor(dispH * dpr);
  const ctx = cv.getContext("2d");
  if (ctx === null) return;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, dispW, dispH);
  ctx.translate(offX, offY);

  const sx = dispW / ps[0];
  const sy = dispH / ps[1];

  const arr = renderLines.value;
  const hoverIdx = hoveredLineIdx.value;
  const hasHover =
    hoverIdx !== null && hoverIdx !== undefined && arr[hoverIdx] !== undefined;

  // pass 1: other boxes — dimmed while hovering, normal otherwise.
  ctx.lineJoin = "miter";
  for (let i = 0; i < arr.length; i++) {
    if (i === hoverIdx) continue;
    const ln = arr[i];
    const box = ln?.box;
    if (ln === undefined || !Array.isArray(box) || box.length < 8) continue;
    tracePath(ctx, box, sx, sy);
    ctx.lineWidth = 1;
    ctx.strokeStyle = confColor(ln.conf);
    ctx.globalAlpha = hasHover ? 0.25 : 1.0;
    ctx.stroke();
  }
  ctx.globalAlpha = 1.0;

  // pass 2: hovered box — accent highlight + translucent fill.
  if (hasHover) {
    const ln = arr[hoverIdx];
    const box = ln?.box;
    if (Array.isArray(box) && box.length >= 8) {
      tracePath(ctx, box, sx, sy);
      ctx.fillStyle = "rgba(124, 108, 240, 0.18)";
      ctx.fill();

      tracePath(ctx, box, sx, sy);
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = "#a594ff";
      ctx.stroke();
    }
  }
}

function onImgLoad(): void {
  imgLoadErr.value = false;
  void nextTick(drawBoxes);
}
function onImgError(): void {
  imgLoadErr.value = true;
}

// resize → redraw
let ro: ResizeObserver | null = null;
onMounted(() => {
  if (typeof ResizeObserver !== "undefined" && imgEl.value !== null) {
    ro = new ResizeObserver(() => drawBoxes());
    ro.observe(imgEl.value);
  }
});
onBeforeUnmount(() => {
  if (ro !== null) {
    try {
      ro.disconnect();
    } catch {
      // ignore
    }
    ro = null;
  }
});

watch(
  () => [renderLines.value, hoveredLineIdx.value, props.pageSize, props.imageUrl],
  () => {
    void nextTick(drawBoxes);
  },
);

// ── line list interaction ─────────────────────────────────────────────────
function onHoverLine(idx: number): void {
  hoveredLineIdx.value = idx;
  scrollHoveredBoxIntoView(idx);
}
function onLeaveLine(): void {
  hoveredLineIdx.value = null;
}

// When hovering a list row, smoothly scroll the image container so the matching
// box is centred (parity with V1 `_scrollHoveredBoxIntoView`).
function scrollHoveredBoxIntoView(idx: number): void {
  const im = imgEl.value;
  if (im === null) return;
  const ln = renderLines.value[idx];
  const box = ln?.box;
  const ps = props.pageSize;
  if (!Array.isArray(box) || box.length < 8 || ps === null) return;
  const wrap = im.closest(".ab-ocr-canvas-wrap") as HTMLElement | null;
  if (wrap === null) return;
  if (
    wrap.scrollHeight <= wrap.clientHeight + 1 &&
    wrap.scrollWidth <= wrap.clientWidth + 1
  ) {
    return;
  }

  const sx = im.clientWidth / ps[0];
  const sy = im.clientHeight / ps[1];
  const xs = [box[0] ?? 0, box[2] ?? 0, box[4] ?? 0, box[6] ?? 0];
  const ys = [box[1] ?? 0, box[3] ?? 0, box[5] ?? 0, box[7] ?? 0];
  const cx = ((Math.min(...xs) + Math.max(...xs)) / 2) * sx;
  const cy = ((Math.min(...ys) + Math.max(...ys)) / 2) * sy;
  const imgRect = im.getBoundingClientRect();
  const wrapRect = wrap.getBoundingClientRect();
  const offsetLeft = imgRect.left - wrapRect.left + wrap.scrollLeft;
  const offsetTop = imgRect.top - wrapRect.top + wrap.scrollTop;
  const targetLeft = offsetLeft + cx - wrap.clientWidth / 2;
  const targetTop = offsetTop + cy - wrap.clientHeight / 2;
  try {
    wrap.scrollTo({ left: targetLeft, top: targetTop, behavior: "smooth" });
  } catch {
    wrap.scrollLeft = targetLeft;
    wrap.scrollTop = targetTop;
  }
}

// ── view mode: image / text ────────────────────────────────────────────────
const viewMode = ref<"image" | "text">("image");

/** Transient "Copied" feedback — index of the line whose copy button was
 *  last clicked (⧉ → ✓ for 1.5s), parity with other copy buttons. */
const copiedLineIdx = ref<number | null>(null);
let copiedLineTimer: number | null = null;

async function copyLine(text: string, idx: number): Promise<void> {
  if (text === "") return;
  try {
    await navigator.clipboard.writeText(text);
    copiedLineIdx.value = idx;
    if (copiedLineTimer !== null) window.clearTimeout(copiedLineTimer);
    copiedLineTimer = window.setTimeout(() => {
      copiedLineIdx.value = null;
      copiedLineTimer = null;
    }, 1500);
  } catch {
    // ignore — copy is best-effort
  }
}

onBeforeUnmount(() => {
  if (copiedLineTimer !== null) window.clearTimeout(copiedLineTimer);
});
</script>

<template>
  <div class="ab-ocr">
    <!-- view switch (segmented, overlaid top-right) -->
    <div
      class="ab-ocr-mode-switch"
      role="tablist"
      :aria-label="t('appBuilder.ocrViewMode')"
    >
      <button
        type="button"
        class="ab-ocr-mode-btn"
        role="tab"
        :class="{ active: viewMode === 'image' }"
        :aria-selected="viewMode === 'image' ? 'true' : 'false'"
        @click="viewMode = 'image'"
      >
        {{ t("appBuilder.ocrModeImage") }}
      </button>
      <button
        type="button"
        class="ab-ocr-mode-btn"
        role="tab"
        :class="{ active: viewMode === 'text' }"
        :aria-selected="viewMode === 'text' ? 'true' : 'false'"
        @click="viewMode = 'text'"
      >
        {{ t("appBuilder.ocrModeText") }}
      </button>
    </div>

    <!-- image mode: image left + lines right -->
    <template v-if="viewMode === 'image'">
      <div class="ab-ocr-split">
        <!-- left: image + box overlay (only when source image available) -->
        <div
          v-if="hasImage"
          class="ab-ocr-canvas-wrap"
        >
          <div class="ab-ocr-img-container">
            <img
              ref="imgEl"
              class="ab-ocr-img"
              :src="imageUrl"
              :alt="t('appBuilder.aria.ocrSource')"
              @load="onImgLoad"
              @error="onImgError"
            />
            <canvas
              v-show="!imgLoadErr"
              ref="canvasEl"
              class="ab-ocr-overlay"
              aria-hidden="true"
            ></canvas>
          </div>
          <div
            v-if="imgLoadErr"
            class="ab-ocr-err"
          >
            {{ t("appBuilder.imageLoadError") }}
          </div>
        </div>

        <!-- right: lines list -->
        <ul
          class="ab-ocr-lines ab-ocr-lines--side"
          @mouseleave="onLeaveLine"
        >
          <li
            v-for="(ln, i) in renderLines"
            :key="i"
            class="ab-ocr-line"
            :class="{ hovered: hoveredLineIdx === i }"
            @mouseenter="onHoverLine(i)"
          >
            <span class="ab-ocr-idx">[{{ ln.line_idx !== undefined ? ln.line_idx : i }}]</span>
            <span class="ab-ocr-text">{{ ln.text }}</span>
            <span
              v-if="typeof ln.conf === 'number'"
              class="ab-ocr-conf"
              :class="{
                high: ln.conf >= 0.8,
                mid: ln.conf >= 0.5 && ln.conf < 0.8,
                low: ln.conf < 0.5,
              }"
            >
              {{ (ln.conf * 100).toFixed(0) }}%
            </span>
            <button
              type="button"
              class="ab-ocr-copy"
              :title="copiedLineIdx === i ? t('appBuilder.copied') : t('appBuilder.copyLine')"
              @click.stop="copyLine(ln.text, i)"
            >
              {{ copiedLineIdx === i ? "✓" : "⧉" }}
            </button>
          </li>
          <li
            v-if="renderLines.length === 0"
            class="ab-ocr-empty"
          >
            {{ t("appBuilder.ocrEmpty") }}
          </li>
        </ul>
      </div>
    </template>

    <!-- text mode -->
    <div
      v-else
      class="ab-ocr-text-view"
    >
      <ul
        class="ab-ocr-lines ab-ocr-lines--full"
        @mouseleave="onLeaveLine"
      >
        <li
          v-for="(ln, i) in renderLines"
          :key="i"
          class="ab-ocr-line"
          :class="{ hovered: hoveredLineIdx === i }"
          @mouseenter="onHoverLine(i)"
        >
          <span class="ab-ocr-idx">[{{ ln.line_idx !== undefined ? ln.line_idx : i }}]</span>
          <span class="ab-ocr-text">{{ ln.text }}</span>
          <span
            v-if="typeof ln.conf === 'number'"
            class="ab-ocr-conf"
            :class="{
              high: ln.conf >= 0.8,
              mid: ln.conf >= 0.5 && ln.conf < 0.8,
              low: ln.conf < 0.5,
            }"
          >
            {{ (ln.conf * 100).toFixed(0) }}%
          </span>
          <button
            type="button"
            class="ab-ocr-copy"
            :title="copiedLineIdx === i ? t('appBuilder.copied') : t('appBuilder.copyLine')"
            @click.stop="copyLine(ln.text, i)"
          >
            {{ copiedLineIdx === i ? "✓" : "⧉" }}
          </button>
        </li>
        <li
          v-if="renderLines.length === 0"
          class="ab-ocr-empty"
        >
          {{ t("appBuilder.ocrEmpty") }}
        </li>
      </ul>
    </div>
  </div>
</template>
