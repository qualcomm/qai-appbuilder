<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ClassificationTable — image classification result viewer (V1 parity).
 *
 * V1 source: QAIModelBuilder_v1_pure/frontend/js/components/app-builder/outputs/ClassificationTable.js
 * Renders predictions as a ranked table with:
 *   - rank column `#` (V1 :54)
 *   - top-1 row highlight via `ab-cls-top` (V1 :53)
 *   - `class_N` fallback label when no human-readable label (V1 :56-57)
 *   - confidence value + bar
 *   - `Top {topK} / {numClasses} classes` footer (V1 :68-70)
 *
 * Uses the global `.ab-cls-*` classes from styles/app-builder/app-builder.css
 * (theme-aware `--ab-*` / `--accent` tokens — no hard-coded colors, unlike V1
 * which baked dark colors into the table; AGENTS.md 🟡 obvious-defect fix).
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";

const { t } = useI18n();

interface ClassificationResult {
  label: string;
  confidence: number;
  /** Raw class index (V1 `class_idx`); used for `class_N` fallback display. */
  classIdx?: number;
  /** Whether the model carries a human-readable label (V1 `has_labels`). */
  hasLabel?: boolean;
}

interface Props {
  results: ClassificationResult[];
  /** Number of top predictions returned (V1 `top_k`). */
  topK?: number | null;
  /** Total number of classes the model can predict (V1 `num_classes`). */
  numClasses?: number | null;
}

const props = withDefaults(defineProps<Props>(), {
  topK: null,
  numClasses: null,
});

function pct(score: number): string {
  return (score * 100).toFixed(1);
}

// V1 ClassificationTable.js:30 — top_k defaults to the row count.
const effectiveTopK = computed<number>(
  () => props.topK ?? props.results.length,
);
</script>

<template>
  <div class="ab-classification-table">
    <table class="ab-cls-table">
      <thead>
        <tr>
          <th class="ab-cls-rank">#</th>
          <th class="ab-cls-label">{{ t("appBuilder.classification.label") }}</th>
          <th class="ab-cls-score">{{ t("appBuilder.classification.confidence") }}</th>
          <th class="ab-cls-bar"></th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="(p, i) in results"
          :key="i"
          :class="{ 'ab-cls-top': i === 0 }"
        >
          <td class="ab-cls-rank">{{ i + 1 }}</td>
          <td class="ab-cls-label">
            <span
              v-if="p.hasLabel !== false && p.label && !p.label.startsWith('class_')"
              class="ab-cls-name"
            >{{ p.label }}</span>
            <span
              v-else
              class="ab-cls-idx"
            >{{ p.classIdx !== undefined ? `class_${p.classIdx}` : p.label }}</span>
          </td>
          <td class="ab-cls-score">{{ pct(p.confidence) }}%</td>
          <td class="ab-cls-bar">
            <div class="ab-cls-bar-bg">
              <div
                class="ab-cls-bar-fill"
                :style="{ width: `${pct(p.confidence)}%` }"
              ></div>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
    <div
      v-if="numClasses"
      class="ab-cls-meta"
    >
      {{ t("appBuilder.classification.topKMeta", { topK: effectiveTopK, numClasses }) }}
    </div>
  </div>
</template>
