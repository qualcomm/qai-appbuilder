<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * DetectionViewer — Object detection bounding box overlay (V1 parity).
 *
 * Renders bounding boxes overlaid on the input image with hover
 * interactions (highlight bbox + table row) and a summary table below.
 *
 * V1 source: QAIModelBuilder_v1_pure/frontend/js/components/app-builder/outputs/DetectionViewer.js
 */
import { computed, ref, onMounted, onBeforeUnmount, nextTick } from "vue";
import { useI18n } from "vue-i18n";

const { t } = useI18n();

// ── Types ────────────────────────────────────────────────────────────────────

export interface Detection {
  label: string;
  confidence: number;
  bbox: [number, number, number, number]; // [x1, y1, x2, y2] pixel or normalized
}

interface Props {
  imageUrl: string;
  detections: Detection[];
}

const props = defineProps<Props>();

// ── Color palette (V1 BOX_COLORS — 20 distinct per-class colors) ─────────────

const BOX_COLORS = [
  "#FF3838", "#FF9D97", "#FF701F", "#FFB21D", "#CFD231",
  "#48F90A", "#92CC17", "#3DDB86", "#1A9334", "#00D4BB",
  "#2C99A8", "#00C2FF", "#344593", "#6473FF", "#0018EC",
  "#8438FF", "#520085", "#CB38FF", "#FF95C8", "#FF37C7",
] as const;

// ── State ────────────────────────────────────────────────────────────────────

const hoveredIdx = ref(-1);
const imgEl = ref<HTMLImageElement | null>(null);
const naturalSize = ref<[number, number]>([0, 0]);

function onImageLoad(): void {
  if (imgEl.value) {
    naturalSize.value = [imgEl.value.naturalWidth, imgEl.value.naturalHeight];
  }
}

// ── Computed ─────────────────────────────────────────────────────────────────

/** Assign each unique label a stable color index. */
const labelColorMap = computed<Map<string, number>>(() => {
  const map = new Map<string, number>();
  let idx = 0;
  for (const det of props.detections) {
    if (!map.has(det.label)) {
      map.set(det.label, idx++);
    }
  }
  return map;
});

function getColor(det: Detection): string {
  const idx = labelColorMap.value.get(det.label) ?? 0;
  return BOX_COLORS[idx % BOX_COLORS.length] as string;
}

/**
 * Detect whether bbox values are normalized (0-1 range) or pixel coordinates.
 * If any value > 1 in any detection → pixel mode; else normalized.
 */
const isPixelCoords = computed<boolean>(() =>
  props.detections.some(
    (d) => d.bbox[0] > 1 || d.bbox[1] > 1 || d.bbox[2] > 1 || d.bbox[3] > 1,
  ),
);

/** Convert bbox to percentages (left, top, width, height) relative to image. */
function boxStyle(det: Detection): Record<string, string> {
  const [x1, y1, x2, y2] = det.bbox;
  let left: number, top: number, width: number, height: number;

  if (isPixelCoords.value) {
    const [w, h] = naturalSize.value;
    if (!w || !h) return { display: "none" };
    left = (x1 / w) * 100;
    top = (y1 / h) * 100;
    width = ((x2 - x1) / w) * 100;
    height = ((y2 - y1) / h) * 100;
  } else {
    // Normalized [x1, y1, x2, y2] in 0-1
    left = x1 * 100;
    top = y1 * 100;
    width = (x2 - x1) * 100;
    height = (y2 - y1) * 100;
  }

  return {
    position: "absolute",
    left: `${left}%`,
    top: `${top}%`,
    width: `${width}%`,
    height: `${height}%`,
  };
}

function labelText(det: Detection): string {
  return `${det.label} ${(det.confidence * 100).toFixed(1)}%`;
}

function pct(score: number): string {
  return (score * 100).toFixed(1);
}

function coordStr(det: Detection): string {
  const [x1, y1, x2, y2] = det.bbox;
  return `[${Math.round(x1)}, ${Math.round(y1)}, ${Math.round(x2)}, ${Math.round(y2)}]`;
}
</script>

<template>
  <div class="ab-det">
    <!-- Image with bounding box overlays -->
    <div
      v-if="imageUrl"
      class="ab-det__canvas"
    >
      <img
        ref="imgEl"
        :src="imageUrl"
        class="ab-det__image"
        :alt="t('appBuilder.aria.detectionInput')"
        @load="onImageLoad"
      />
      <!-- Bounding boxes -->
      <div
        v-for="(det, i) in detections"
        :key="i"
        class="ab-det__box"
        :style="{
          ...boxStyle(det),
          borderColor: getColor(det),
          opacity: hoveredIdx === -1 || hoveredIdx === i ? 1 : 0.3,
        }"
        @mouseenter="hoveredIdx = i"
        @mouseleave="hoveredIdx = -1"
      >
        <span
          class="ab-det__badge"
        >{{ labelText(det) }}</span>
      </div>
    </div>

    <!-- Summary info -->
    <div class="ab-det__summary">
      {{ detections.length }} detection{{ detections.length !== 1 ? "s" : "" }}
      <span v-if="naturalSize[0]">
        &middot; {{ naturalSize[0] }}&times;{{ naturalSize[1] }}px
      </span>
    </div>

    <!-- Detection table -->
    <div
      v-if="detections.length"
      class="ab-det__table-wrap"
    >
      <table class="ab-det__table">
        <thead>
          <tr>
            <th>#</th>
            <th>{{ t("appBuilder.detection.class") }}</th>
            <th>{{ t("appBuilder.detection.confidence") }}</th>
            <th>{{ t("appBuilder.detection.box") }}</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="(det, i) in detections"
            :key="i"
            class="ab-det__row"
            :class="{ 'ab-det__row--hovered': hoveredIdx === i }"
            @mouseenter="hoveredIdx = i"
            @mouseleave="hoveredIdx = -1"
          >
            <td>
              <span
                class="ab-det__dot"
                :style="{ backgroundColor: getColor(det) }"
              ></span>
              {{ i + 1 }}
            </td>
            <td>{{ det.label }}</td>
            <td>{{ pct(det.confidence) }}%</td>
            <td class="ab-det__coords">
              {{ coordStr(det) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.ab-det {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ab-det__canvas {
  position: relative;
  display: inline-block;
  max-width: 100%;
  line-height: 0;
}

.ab-det__image {
  max-width: 100%;
  height: auto;
  display: block;
  border-radius: 4px;
}

.ab-det__box {
  position: absolute;
  border: 2px solid;
  box-sizing: border-box;
  pointer-events: auto;
  transition: opacity 0.15s;
  cursor: pointer;
}

.ab-det__badge {
  position: absolute;
  top: -1px;
  left: -1px;
  background: rgba(0, 0, 0, 0.7);
  color: #fff;
  font-size: 11px;
  line-height: 1;
  padding: 2px 5px;
  border-radius: 2px;
  white-space: nowrap;
  pointer-events: none;
}

.ab-det__summary {
  color: var(--text-secondary);
  font-size: 12px;
}

.ab-det__table-wrap {
  overflow-x: auto;
}

.ab-det__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  color: var(--text-primary);
}

.ab-det__table th {
  padding: 6px 8px;
  color: var(--text-secondary);
  font-weight: 600;
  text-align: left;
  border-bottom: 1px solid var(--border);
}

.ab-det__table td {
  padding: 5px 8px;
  border-bottom: 1px solid var(--border-light);
}

.ab-det__row {
  cursor: pointer;
  transition: background-color 0.15s;
}

.ab-det__row--hovered {
  background-color: var(--bg-hover);
}

.ab-det__dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  margin-right: 6px;
  vertical-align: middle;
}

.ab-det__coords {
  font-family: monospace;
  font-size: 12px;
  color: var(--text-muted);
}
</style>
