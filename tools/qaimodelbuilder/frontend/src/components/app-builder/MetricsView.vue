<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * MetricsView — App Builder performance metrics panel.
 *
 * V1 行为事实来源：`QAIModelBuilder_v1_pure/frontend/js/components/app-builder/MetricsView.js`
 * （完整 500 行；headline + 彩色 stage 堆叠条 + Device/Quantization 双列 +
 * Details 折叠区（stage 明细 + Model Load 一次性阶段 + p50/p90/std/peak memory/
 * model size）+ Aggregated 服务端聚合区块（latency typical/slow10/slowest 三列 +
 * memory avg/max + 用户评分 👍👎 + qualityScore） + sendToChat 提示 + 空态）。
 *
 * V2 实现策略（顶部"两条判据"）：
 *   - **判据 2（用户感知对齐 V1）**：所有 V1 子视图均就位 ——
 *       ① Headline latency
 *       ② 彩色 stage 堆叠条（per-segment 颜色 / pct / tooltip）
 *       ③ Device / Quantization 双列
 *       ④ Details 折叠（<details><summary>）含 stage 明细 + Model Load + p50/p90/
 *          std + peak memory + model size
 *       ⑤ Aggregated（服务端 GET /api/appbuilder/metrics/{model_id}）：count +
 *          Typical/Slow(10%)/Slowest 三列 + Memory avg/max + User Rating
 *       ⑥ Send-to-chat 提示
 *       ⑦ 历史 sparkline + p50/p95/max（V2 既有，保留）
 *   - **判据 1（架构更优）**：
 *       - Stage palette / formatMs / formatMB / aggregated fetch 都拆出
 *         composable（`useMetricsHistory` / `useAggregatedMetrics`）；模板纯渲染。
 *       - TS 严格类型；新 props 都可选 — 父未传则区块隐藏（无运行时报错）。
 *       - `apiJson` 替代 V1 裸 `window.fetch`（CSRF / 错误解析 / base-url 集中）。
 *       - V1 的 inline `STAGE_DISPLAY_NAMES` map + 两个 `fmt*` 函数搬到 module 顶部
 *         的纯函数（无副作用、可单测），不照搬 V1 巨石组件。
 *
 * 数据缺口（待后端 / 待主 Agent 派单）：
 *   V2 后端 `RunMetricsResponse`（apps/api/.../app_builder.py）当前仅暴露
 *   `run_id / status / artifact_count / duration_ms / started_at / finished_at /
 *   error_message`，不含 V1 的 stages / loadStages / latencyDistribution /
 *   memoryMB / device / model.runtime.modelSizeMB。
 *   并且 GET `/api/appbuilder/metrics/{model_id}` aggregated 端点尚未实装。
 *   本组件已按 V1 schema 全量布好渲染：父组件传 `currentRun` / 后端补 endpoint
 *   后即自动显示对应区块；当前未传时所有"扩展"区块自动隐藏（友好降级，不影响
 *   既有 sparkline + KV rows 渲染）。
 */
import { computed, watch } from "vue";
import { useI18n } from "vue-i18n";

import {
  formatMB,
  formatMs,
  useMetricsHistory,
} from "@/composables/app-builder/useMetricsHistory";
import { useAggregatedMetrics } from "@/composables/app-builder/useAggregatedMetrics";
import type { AppRun } from "@/stores/appBuilder";

// ── Stage palette (V1 STAGE_DISPLAY_NAMES + color map) ─────────────────────
//
// V1 没有显式的 stage→color 静态表（颜色由 CSS class `ab-stage--<prefix>` 决定）；
// V2 这里提供一个静态 fallback palette（命中 V1 已知的 stage prefix），未命中时
// 用 stage name 的稳定哈希派生 HSL 色相，保证视觉稳定。同名 stage 永远同色。
//
// STAGE_DISPLAY_NAMES 只保留模型架构专有名（BERT / Encoder / Flow / Decoder /
// Attention / Text → Phoneme）——这些不翻译。可译的展示名（Text Processing /
// NPU Models / Post-process）由下方 setup 里的 `stageLabel` 走 i18n
// (`appBuilder.stageNames.*`)，按 raw key 覆盖。
const STAGE_DISPLAY_NAMES: Readonly<Record<string, string>> = {
  g2p: "Text → Phoneme",
  bert_infer: "BERT",
  encoder_infer: "Encoder",
  attn: "Attention",
  flow_infer: "Flow",
  decoder_infer: "Decoder",
};

/** Raw stage key → i18n key under `appBuilder.stageNames` (translatable names). */
const STAGE_I18N_KEYS: Readonly<Record<string, string>> = {
  postprocess: "appBuilder.stageNames.postprocess",
  npu_model_load: "appBuilder.stageNames.npuModelLoad",
  g2p_warmup: "appBuilder.stageNames.textProcessing",
};

const STAGE_COLOR_PALETTE: Readonly<Record<string, string>> = {
  g2p: "#5b8def",
  bert: "#7c5fe6",
  encoder: "#3aa8a4",
  attn: "#e08a3c",
  flow: "#48b06f",
  decoder: "#d65a8e",
  postprocess: "#8a939d",
  npu: "#11a37f",
  default: "#7185a3",
};

function stagePrefix(name: string): string {
  return name.split("_")[0] ?? name;
}

/** Display name (V1 fallback: capitalize the raw key). */
function stageName(raw: string): string {
  if (STAGE_DISPLAY_NAMES[raw] !== undefined) return STAGE_DISPLAY_NAMES[raw];
  return raw.charAt(0).toUpperCase() + raw.slice(1);
}

/** Stable hash → HSL hue, so the same stage always gets the same color. */
function hashHue(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i += 1) {
    h = (h * 31 + s.charCodeAt(i)) >>> 0;
  }
  return h % 360;
}

function stageColor(name: string, override?: string | null): string {
  if (override !== undefined && override !== null && override !== "") return override;
  const palette = STAGE_COLOR_PALETTE[stagePrefix(name)];
  if (palette !== undefined) return palette;
  return `hsl(${hashHue(name)}, 55%, 55%)`;
}

// ── Types ───────────────────────────────────────────────────────────────────

interface MetricRow {
  label: string;
  value: string | number;
  unit?: string;
}

/**
 * V1-compatible per-stage telemetry record. `pct` is recomputed view-side so
 * the prop only needs `name + latencyMs`.
 */
export interface StageEntry {
  name: string;
  latencyMs: number;
  /** Optional sub-model label (e.g. "encoder.bin"). */
  model?: string | null;
  /** Optional explicit color override (CSS color string). */
  color?: string | null;
}

/** Optional latency distribution carried in `metrics.latencyDistribution`. */
export interface LatencyDistribution {
  p50?: number | null;
  p90?: number | null;
  std?: number | null;
}

/**
 * V1-compatible "current run" metrics envelope. Optional everywhere — V2
 * backend currently emits a subset; missing fields just hide the related
 * sub-views (no runtime error).
 */
export interface CurrentRunMetrics {
  /** Per-stage breakdown for the colored stacked bar + Stage list. */
  stages?: StageEntry[] | null;
  /** One-time Model Load stages (separate section in Details). */
  loadStages?: StageEntry[] | null;
  /** Display device string ("CPU" / "GPU" / "QNN" / "HTP" / …). */
  device?: string | null;
  /** Quantization label (FP16 / INT8 / …). */
  quantization?: string | null;
  /** Peak memory in MB. */
  peakMemoryMB?: number | null;
  /** Model size in MB. */
  modelSizeMB?: number | null;
  /** Latency distribution (p50 / p90 / std). */
  latencyDistribution?: LatencyDistribution | null;
  /** Optional latency headline override (defaults to last sample). */
  latencyMs?: number | null;
}

interface Props {
  /** Key/value rows produced by the overlay (latency / device / 评分 …). */
  metrics: MetricRow[];
  /**
   * Optional run history (newest first). When provided, the sparkline +
   * p50/p95/max stats are rendered. When omitted those sections are hidden
   * (avoids "empty trend" noise on views without history).
   */
  runs?: AppRun[] | null;
  /** Section title; defaults to the i18n metrics title. */
  title?: string;
  /**
   * Optional V1-compatible current-run telemetry (stages / loadStages / device /
   * quantization / peakMemoryMB / modelSizeMB / latencyDistribution). When
   * absent, the colored stacked bar + Details folded section are hidden so
   * existing views (which only pass `metrics + runs`) keep their current look.
   */
  currentRun?: CurrentRunMetrics | null;
  /**
   * Optional model id; when provided, the Aggregated section fetches
   * `/api/app-builder/metrics/model/{modelId}` (3-M1, V1 parity). When a
   * model has no aggregated history yet the composable shows the
   * "Pending backend data" placeholder (hidden when empty).
   */
  modelId?: string | null;
  /** Optional variant id, forwarded as `?variant_id=...` to the aggregated endpoint. */
  variantId?: string | null;
  /**
   * Optional rating signal — when this changes (user submits 👍/👎), the
   * Aggregated section auto-refreshes (V1 MetricsView.js:294-300 parity).
   */
  rating?: number | null;
}

const props = withDefaults(defineProps<Props>(), {
  runs: null,
  title: "",
  currentRun: null,
  modelId: null,
  variantId: null,
  rating: null,
});

const { t } = useI18n();

/**
 * Stage display name with i18n overlay: translatable stage names
 * (Text Processing / NPU Models / Post-process) go through `t`; model
 * architecture names (BERT / Encoder / …) stay as the static fallback.
 */
function stageLabel(raw: string): string {
  const i18nKey = STAGE_I18N_KEYS[raw];
  if (i18nKey !== undefined) return t(i18nKey);
  return stageName(raw);
}

const headerTitle = computed<string>(
  () => props.title || t("appBuilder.metrics.title"),
);

// ── History projection (composable does all the math) ───────────────────────
const history = useMetricsHistory(() => props.runs);
const showSparkline = computed<boolean>(() => history.count.value >= 1);

// ── Sparkline geometry (handwritten SVG, V1 parity) ─────────────────────────
const SPARK_W = 240;
const SPARK_H = 56;
const SPARK_PAD = 4;

interface SparkPoint {
  x: number;
  y: number;
  v: number;
}

const sparkPoints = computed<SparkPoint[]>(() => {
  const samples = history.points.value;
  if (samples.length === 0) return [];
  const innerW = SPARK_W - SPARK_PAD * 2;
  const innerH = SPARK_H - SPARK_PAD * 2;
  const lo = Math.min(...samples);
  const hi = Math.max(...samples);
  const range = hi - lo || 1;
  if (samples.length === 1) {
    const v = samples[0] ?? 0;
    return [{ x: SPARK_PAD + innerW, y: SPARK_PAD + innerH / 2, v }];
  }
  return samples.map((v, i) => {
    const x = SPARK_PAD + (i / (samples.length - 1)) * innerW;
    const norm = (v - lo) / range;
    const y = SPARK_PAD + innerH - norm * innerH;
    return { x, y, v };
  });
});

const sparkPath = computed<string>(() => {
  const pts = sparkPoints.value;
  if (pts.length === 0) return "";
  return pts
    .map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(2)} ${p.y.toFixed(2)}`)
    .join(" ");
});

const sparkAreaPath = computed<string>(() => {
  const pts = sparkPoints.value;
  if (pts.length < 2) return "";
  const first = pts[0]!;
  const last = pts[pts.length - 1]!;
  const top = sparkPath.value;
  return `${top} L ${last.x.toFixed(2)} ${(SPARK_H - SPARK_PAD).toFixed(2)} L ${first.x.toFixed(2)} ${(SPARK_H - SPARK_PAD).toFixed(2)} Z`;
});

// ── Stats text (formatted via composable helpers) ───────────────────────────
const p50Text = computed<string>(() => formatMs(history.p50.value));
const p95Text = computed<string>(() => formatMs(history.p95.value));
const maxText = computed<string>(() => formatMs(history.max.value));

// ── Stage projection (V1 parity: compute pct = lat / total * 100). ──────────

interface StageView extends StageEntry {
  pct: number;
  formatted: string;
  color: string;
}

const stageViews = computed<StageView[]>(() => {
  const arr = props.currentRun?.stages;
  if (!Array.isArray(arr) || arr.length === 0) return [];
  const total = arr.reduce(
    (sum, s) => sum + (Number.isFinite(s.latencyMs) ? s.latencyMs : 0),
    0,
  );
  return arr.map((s) => ({
    ...s,
    pct: total > 0 ? Math.round((s.latencyMs / total) * 100) : 0,
    formatted: formatMs(s.latencyMs),
    color: stageColor(s.name, s.color ?? null),
  }));
});

const loadStageViews = computed<Array<StageEntry & { formatted: string }>>(() => {
  const arr = props.currentRun?.loadStages;
  if (!Array.isArray(arr) || arr.length === 0) return [];
  return arr.map((s) => ({ ...s, formatted: formatMs(s.latencyMs) }));
});

const hasStages = computed<boolean>(() => stageViews.value.length > 0);
const hasLoadStages = computed<boolean>(() => loadStageViews.value.length > 0);

// ── Current-run scalar fields ───────────────────────────────────────────────

const peakMemoryText = computed<string>(() =>
  formatMB(props.currentRun?.peakMemoryMB),
);
const modelSizeText = computed<string>(() =>
  formatMB(props.currentRun?.modelSizeMB),
);

const distP50Text = computed<string>(() =>
  formatMs(props.currentRun?.latencyDistribution?.p50),
);
const distP90Text = computed<string>(() =>
  formatMs(props.currentRun?.latencyDistribution?.p90),
);
const distStdText = computed<string>(() => {
  const std = props.currentRun?.latencyDistribution?.std;
  const n = Number(std);
  return Number.isFinite(n) ? `${n.toFixed(2)} ms` : "—";
});

const hasPeakMemory = computed<boolean>(
  () => Number.isFinite(Number(props.currentRun?.peakMemoryMB)),
);
const hasModelSize = computed<boolean>(
  () => Number.isFinite(Number(props.currentRun?.modelSizeMB)),
);
const hasDistP50 = computed<boolean>(
  () => Number.isFinite(Number(props.currentRun?.latencyDistribution?.p50)),
);
const hasDistP90 = computed<boolean>(
  () => Number.isFinite(Number(props.currentRun?.latencyDistribution?.p90)),
);
const hasDistStd = computed<boolean>(
  () => Number.isFinite(Number(props.currentRun?.latencyDistribution?.std)),
);

const hasDetails = computed<boolean>(
  () =>
    hasStages.value ||
    hasLoadStages.value ||
    hasPeakMemory.value ||
    hasModelSize.value ||
    hasDistP50.value ||
    hasDistP90.value ||
    hasDistStd.value,
);

// ── Aggregated server-side metrics (V1 MetricsView.js:255-300 parity) ───────

const aggregated = useAggregatedMetrics();

watch(
  () => [props.modelId, props.variantId, props.rating] as const,
  ([modelId, variantId]) => {
    if (modelId === null || modelId === undefined || modelId === "") {
      aggregated.reset();
      return;
    }
    void aggregated.fetch(modelId, variantId);
  },
  { immediate: true },
);

const aggData = computed(() => aggregated.data.value);
const aggLoading = computed(() => aggregated.loading.value);
const aggError = computed(() => aggregated.error.value);

const showAggregated = computed<boolean>(
  () => aggData.value !== null && (aggData.value?.count ?? 0) > 0,
);
const showAggLoading = computed<boolean>(
  () => aggLoading.value && aggData.value === null,
);
const showAggPending = computed<boolean>(
  () => aggError.value === "pendingBackend",
);

const aggBasedOnText = computed<string>(() =>
  t("appBuilder.metrics.basedOn", { count: aggData.value?.count ?? 0 }),
);

const aggLatencyTypical = computed<string>(() =>
  formatMs(aggData.value?.latencyMs?.p50),
);
const aggLatencySlow10 = computed<string>(() =>
  formatMs(aggData.value?.latencyMs?.p90),
);
const aggLatencySlowest = computed<string>(() =>
  formatMs(aggData.value?.latencyMs?.max),
);

const aggMemoryAvg = computed<string>(() => formatMB(aggData.value?.memoryMB?.mean));
const aggMemoryMax = computed<string>(() => formatMB(aggData.value?.memoryMB?.max));

const showAggLatency = computed<boolean>(() => {
  const lat = aggData.value?.latencyMs;
  return lat !== null && lat !== undefined && Number.isFinite(Number(lat?.p50));
});
const showAggMemory = computed<boolean>(() => {
  const mem = aggData.value?.memoryMB;
  return mem !== null && mem !== undefined && Number.isFinite(Number(mem?.mean));
});
const showAggRating = computed<boolean>(() => {
  const r = aggData.value?.rating;
  return r !== null && r !== undefined && (r?.count ?? 0) > 0;
});

const aggQualityText = computed<string>(() => {
  const q = aggData.value?.rating?.qualityScore;
  const n = Number(q);
  return Number.isFinite(n) ? `${(n * 100).toFixed(0)}%` : "—";
});

function onRefreshAggregated(): void {
  void aggregated.refresh();
}

// ── Headline latency (V1 MetricsView.js:366-369 parity) ─────────────────────
// The 32px mono headline shows the current run latency. Sourced from
// `currentRun.latencyMs`; hidden when absent so views that only pass KV rows +
// history keep their lean look.
const hasHeadline = computed<boolean>(() =>
  Number.isFinite(Number(props.currentRun?.latencyMs)),
);
const headlineLatencyText = computed<string>(() =>
  formatMs(props.currentRun?.latencyMs),
);

// ── Send-to-chat hint ───────────────────────────────────────────────────────
const chatHint = computed<string>(() => t("appBuilder.metrics.sendToChatHint"));

function renderRow(row: MetricRow): string {
  const v = row.value;
  if (v === null || v === undefined || v === "") return "—";
  return row.unit ? `${v} ${row.unit}` : String(v);
}

const hasRows = computed<boolean>(() => props.metrics.length > 0);
</script>

<template>
  <section
    class="ab-metrics"
    data-testid="app-builder-metrics-view"
  >
    <div class="ab-metrics-section-title">
      {{ headerTitle }}
    </div>

    <!-- ① Empty state — when neither current rows nor history nor aggregated. -->
    <div
      v-if="!hasRows && !showSparkline && !hasStages && !showAggregated && !hasHeadline"
      class="ab-metrics-empty"
    >
      {{ t("appBuilder.metrics.idle") }}
    </div>

    <template v-else>
      <!-- ⓪ Headline — 32px mono latency (V1 MetricsView.js:366-369). -->
      <div
        v-if="hasHeadline"
        class="ab-metrics-headline"
        data-testid="app-builder-metrics-headline"
      >
        <div class="ab-metrics-headline-value">
          {{ headlineLatencyText }}
        </div>
        <div class="ab-metrics-headline-label">
          {{ t("appBuilder.metrics.latency") }}
        </div>
      </div>

      <!-- ② Key/value rows (latency / device / quantization / …). -->
      <dl
        v-if="hasRows"
        class="ab-metrics-kv"
      >
        <div
          v-for="metric in props.metrics"
          :key="metric.label"
          class="ab-metrics-kv-row"
        >
          <dt class="ab-metrics-row-label">
            {{ metric.label }}
          </dt>
          <dd class="ab-metrics-row-value">
            {{ renderRow(metric) }}
          </dd>
        </div>
      </dl>

      <!-- ③ Colored stage stacked bar (V1 L372-381 parity). Hidden when no stages. -->
      <div
        v-if="hasStages"
        class="ab-metrics-stages"
        data-testid="app-builder-metrics-stages-bar"
      >
        <div
          class="ab-stages-bar"
          role="img"
          :aria-label="t('appBuilder.metrics.stages')"
        >
          <div
            v-for="(s, i) in stageViews"
            :key="i"
            class="ab-stage-segment"
            :style="{ width: `${s.pct}%`, backgroundColor: s.color }"
            :title="`${stageLabel(s.name)}${s.model ? ` (${s.model})` : ''}: ${s.formatted} (${s.pct}%)`"
          ></div>
        </div>
      </div>

      <!-- ④ Details folded section (stage list + Model Load + p50/p90/std + memory + size). -->
      <details
        v-if="hasDetails"
        class="ab-metrics-details"
        data-testid="app-builder-metrics-details"
      >
        <summary>{{ t("appBuilder.metrics.detailsToggle") }}</summary>

        <ul
          v-if="hasStages"
          class="ab-stages-list"
        >
          <li
            v-for="(s, i) in stageViews"
            :key="i"
            class="ab-stage-item"
          >
            <span
              class="ab-stage-dot"
              :style="{ backgroundColor: s.color }"
            ></span>
            <span class="ab-stage-name">{{ stageLabel(s.name) }}</span>
            <span
              v-if="s.model"
              class="ab-stage-model"
            >({{ s.model }})</span>
            <span class="ab-stage-time">{{ s.formatted }}</span>
            <span class="ab-stage-pct">{{ s.pct }}%</span>
          </li>
        </ul>

        <div
          v-if="hasLoadStages"
          class="ab-metrics-load-section"
        >
          <div class="ab-metrics-load-header">
            {{ t("appBuilder.metrics.modelLoad") }}
          </div>
          <ul class="ab-stages-list ab-stages-list--load">
            <li
              v-for="(ls, i) in loadStageViews"
              :key="`ld${i}`"
              class="ab-stage-item ab-stage-item--load"
            >
              <span class="ab-stage-name">{{ stageLabel(ls.name) }}</span>
              <span class="ab-stage-time">{{ ls.formatted }}</span>
            </li>
          </ul>
        </div>

        <dl
          v-if="hasDistP50 || hasDistP90 || hasDistStd || hasPeakMemory || hasModelSize"
          class="ab-metrics-kv ab-metrics-kv--devel"
        >
          <div
            v-if="hasDistP50"
            class="ab-metrics-kv-row"
          >
            <dt>p50</dt>
            <dd>{{ distP50Text }}</dd>
          </div>
          <div
            v-if="hasDistP90"
            class="ab-metrics-kv-row"
          >
            <dt>p90</dt>
            <dd>{{ distP90Text }}</dd>
          </div>
          <div
            v-if="hasDistStd"
            class="ab-metrics-kv-row"
          >
            <dt>std</dt>
            <dd>{{ distStdText }}</dd>
          </div>
          <div
            v-if="hasPeakMemory"
            class="ab-metrics-kv-row"
          >
            <dt>{{ t("appBuilder.metrics.peakMemory") }}</dt>
            <dd>{{ peakMemoryText }}</dd>
          </div>
          <div
            v-if="hasModelSize"
            class="ab-metrics-kv-row"
          >
            <dt>{{ t("appBuilder.metrics.modelSize") }}</dt>
            <dd>{{ modelSizeText }}</dd>
          </div>
        </dl>
      </details>

      <!-- ⑤ History sparkline — when at least one prior run is available. -->
      <div
        v-if="showSparkline"
        class="ab-metrics-history"
        data-testid="app-builder-metrics-history"
      >
        <div class="ab-metrics-history-head">
          <span class="ab-metrics-history-title">
            {{ t("appBuilder.metrics.history") }}
          </span>
          <span class="ab-metrics-history-count">
            ({{ history.count.value }} {{ t("appBuilder.metrics.runs") }})
          </span>
        </div>

        <svg
          class="ab-metrics-sparkline"
          :viewBox="`0 0 ${SPARK_W} ${SPARK_H}`"
          :width="SPARK_W"
          :height="SPARK_H"
          role="img"
          :aria-label="t('appBuilder.metrics.sparklineLabel')"
        >
          <defs>
            <linearGradient
              id="ab-metrics-spark-fill"
              x1="0"
              y1="0"
              x2="0"
              y2="1"
            >
              <stop
                offset="0%"
                stop-color="currentColor"
                stop-opacity="0.25"
              />
              <stop
                offset="100%"
                stop-color="currentColor"
                stop-opacity="0"
              />
            </linearGradient>
          </defs>
          <path
            v-if="sparkAreaPath"
            class="ab-metrics-sparkline-area"
            :d="sparkAreaPath"
            fill="url(#ab-metrics-spark-fill)"
          />
          <path
            v-if="sparkPath"
            class="ab-metrics-sparkline-line"
            :d="sparkPath"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
          <circle
            v-for="(p, i) in sparkPoints"
            :key="i"
            class="ab-metrics-sparkline-dot"
            :cx="p.x"
            :cy="p.y"
            r="2"
            fill="currentColor"
          >
            <title>{{ formatMs(p.v) }}</title>
          </circle>
        </svg>

        <dl
          class="ab-metrics-kv ab-metrics-kv--devel"
          data-testid="app-builder-metrics-stats"
        >
          <div class="ab-metrics-kv-row">
            <dt>p50</dt>
            <dd>{{ p50Text }}</dd>
          </div>
          <div class="ab-metrics-kv-row">
            <dt>p95</dt>
            <dd>{{ p95Text }}</dd>
          </div>
          <div class="ab-metrics-kv-row">
            <dt>{{ t("appBuilder.metrics.slowest") }}</dt>
            <dd>{{ maxText }}</dd>
          </div>
        </dl>

        <p
          v-if="history.indeterminate.value"
          class="ab-metrics-history-hint"
        >
          {{ t("appBuilder.metrics.historyIndeterminate") }}
        </p>
      </div>

      <!-- ⑥ Aggregated — server-side stats over the last N successful runs. -->
      <div
        v-if="showAggregated && aggData"
        class="ab-metrics-aggregated"
        data-testid="app-builder-metrics-aggregated"
      >
        <h4 class="ab-metrics-section-title">
          <span>{{ t("appBuilder.metrics.aggregated") }}</span>
          <span class="ab-metrics-aggregated-count">
            ({{ aggBasedOnText }})
          </span>
          <button
            type="button"
            class="ab-metrics-refresh"
            :disabled="aggLoading"
            :title="t('appBuilder.metrics.refresh')"
            :aria-label="t('appBuilder.metrics.refresh')"
            @click="onRefreshAggregated"
          >
            ⟳
          </button>
        </h4>

        <div class="ab-metrics-agg-grid">
          <div
            v-if="showAggLatency"
            class="ab-metrics-agg-table"
          >
            <div class="ab-metrics-agg-thead">
              <span class="ab-metrics-agg-th-label">
                {{ t("appBuilder.metrics.latency") }}
              </span>
              <span class="ab-metrics-agg-th">{{ t("appBuilder.metrics.typical") }}</span>
              <span class="ab-metrics-agg-th">{{ t("appBuilder.metrics.slow10") }}</span>
              <span class="ab-metrics-agg-th">{{ t("appBuilder.metrics.slowest") }}</span>
            </div>
            <div class="ab-metrics-agg-tbody">
              <span class="ab-metrics-agg-th-label"></span>
              <span class="ab-metrics-agg-cell">{{ aggLatencyTypical }}</span>
              <span class="ab-metrics-agg-cell">{{ aggLatencySlow10 }}</span>
              <span class="ab-metrics-agg-cell">{{ aggLatencySlowest }}</span>
            </div>
          </div>

          <div
            v-if="showAggMemory"
            class="ab-metrics-agg-row"
          >
            <span class="ab-metrics-agg-label">{{ t("appBuilder.metrics.memory") }}</span>
            <span class="ab-metrics-agg-value">
              avg: {{ aggMemoryAvg }} | max: {{ aggMemoryMax }}
            </span>
          </div>

          <div
            v-if="showAggRating"
            class="ab-metrics-agg-row"
          >
            <span class="ab-metrics-agg-label">{{ t("appBuilder.metrics.rating") }}</span>
            <span class="ab-metrics-agg-value">
              👍 {{ aggData.rating?.thumbsUp ?? 0 }}
              / 👎 {{ aggData.rating?.thumbsDown ?? 0 }}
              ({{ t("appBuilder.metrics.qualityScore") }}:
              {{ aggQualityText }})
            </span>
          </div>
        </div>
      </div>

      <div
        v-else-if="showAggLoading"
        class="ab-metrics-aggregated-loading"
      >
        {{ t("appBuilder.metrics.loading") }}
      </div>

      <div
        v-else-if="showAggPending"
        class="ab-metrics-aggregated-pending"
      >
        {{ t("appBuilder.metrics.pendingBackend") }}
      </div>

      <!-- ⑦ Bottom: send-to-chat hint (V1 last line of the panel). -->
      <p class="ab-metrics-chat-hint">
        {{ chatHint }}
      </p>
    </template>
  </section>
</template>
