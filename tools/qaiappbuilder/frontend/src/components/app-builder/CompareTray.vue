<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CompareTray — compare multiple run outputs side by side.
 *
 * V1 行为事实来源：QAIModelBuilder_v1_pure/frontend/js/components/app-builder/CompareTray.js
 * 三视图 Cards / Table / Radar（V1:100,288-434），指标提取 / 归一化算法对齐 V1:103-277。
 *
 * 实现方式（V2 更优结构）：用 TS + computed 把"指标提取 / 表格行 / 雷达多边形 / 轴 / 标签"
 * 拆成纯派生数据，模板只负责渲染；不在组件里堆全局 ref / 巨石函数。
 */
import { ref, computed } from "vue";
import { useI18n } from "vue-i18n";

/**
 * V2 CompareItem（由父 overlay 传入，来自 store.compareItems）。
 *
 * 注意：当前 store/overlay 只映射了 `id / modelId / output / metrics{latencyMs,tokens}`，
 * 缺 V1 用于 Table/Radar 的 `modelName / status / metrics.memoryMB / metrics.modelSizeMB /
 * metrics.quantization / metrics.device / runtime / rating / output 结构化字段`。
 * 这里把这些字段声明为 optional，模板按 V1 字段渲染，缺失时兜底 "—"/隐藏；
 * 待主 Agent 在 stores/appBuilder.ts + overlay 映射补齐后，Table/Radar 即有真实数据。
 */
interface CompareMetrics {
  latencyMs?: number | null;
  tokens?: number | null;
  memoryMB?: number | null;
  modelSizeMB?: number | null;
  quantization?: string | null;
  device?: string | null;
}

interface CompareOutputShape {
  predictions?: Array<{ conf?: number; confidence?: number }>;
  lines?: Array<{ conf?: number }>;
  segments?: Array<{ conf?: number }>;
}

interface CompareItem {
  id: string;
  modelId: string;
  modelName?: string;
  output: string | CompareOutputShape | Record<string, unknown> | null;
  metrics?: CompareMetrics;
  status?: string;
  rating?: number;
  variant?: string | null;
  runtime?: { backend?: string | null; quantization?: string | null; modelSizeMB?: number | null };
  modelSizeMB?: number | null;
}

interface Props {
  isOpen: boolean;
  items: CompareItem[];
}

const props = defineProps<Props>();

const emit = defineEmits<{
  "close": [];
  "clear": [];
  "remove-run": [id: string];
}>();

const { t } = useI18n();

// 'cards' | 'table' | 'radar'（V1:100）
type ViewMode = "cards" | "table" | "radar";
const viewMode = ref<ViewMode>("cards");

const count = computed(() => props.items.length);

// ---------------------------------------------------------------------
// 指标提取 / 派生（best-effort，缺字段兜底） — 对齐 V1:103-154
// ---------------------------------------------------------------------
interface ExtractedMetrics {
  latencyMs: number;
  memoryMB: number;
  modelSizeMB: number;
  quantization: string;
  runtime: string;
}

function extractMetrics(run: CompareItem): ExtractedMetrics {
  const m = run.metrics ?? {};
  const runtimeText =
    m.device ??
    (run.runtime?.backend ? String(run.runtime.backend).toUpperCase() : "—");
  return {
    latencyMs: Number(m.latencyMs) || 0,
    memoryMB: Number(m.memoryMB) || 0,
    modelSizeMB:
      Number(m.modelSizeMB ?? run.modelSizeMB ?? run.runtime?.modelSizeMB) || 0,
    quantization: m.quantization ?? run.runtime?.quantization ?? "—",
    runtime: runtimeText,
  };
}

// 从 run.output 算平均置信度（任务相关） — 对齐 V1:127-147
function avgConf(run: CompareItem): number | null {
  const o = run.output;
  if (!o || typeof o === "string") return null;
  if (Array.isArray(o.predictions) && o.predictions.length) {
    const first = o.predictions[0];
    const c = Number(first?.conf != null ? first.conf : first?.confidence);
    return Number.isFinite(c) ? c : null;
  }
  if (Array.isArray(o.lines) && o.lines.length) {
    const vs = o.lines.map((l) => Number(l?.conf)).filter(Number.isFinite);
    return vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : null;
  }
  if (Array.isArray(o.segments) && o.segments.length) {
    const vs = o.segments.map((s) => Number(s?.conf)).filter(Number.isFinite);
    return vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : null;
  }
  return null;
}

// 用户评分：rating（-1=👎, 0=neutral, 1=👍），缺失 null — 对齐 V1:150-154
function userScore(run: CompareItem): number | null {
  if (run.rating == null) return null;
  const v = Number(run.rating);
  return Number.isFinite(v) ? v : null;
}

// 输出预览（截断 600 字符） — 对齐 V1:85-95
function previewOutput(output: CompareItem["output"]): string {
  let s: string;
  try {
    s = typeof output === "string" ? output : JSON.stringify(output, null, 2);
  } catch {
    s = String(output);
  }
  if (s == null) return "";
  return s.length > 600 ? s.slice(0, 600) + "..." : s;
}

// ---------------------------------------------------------------------
// Table 视图行（含格式化文本） — 对齐 V1:157-174
// ---------------------------------------------------------------------
const tableRows = computed(() =>
  props.items.map((r) => {
    const m = extractMetrics(r);
    const conf = avgConf(r);
    const us = userScore(r);
    const runtimePieces = [m.runtime, m.quantization].filter(
      (x) => x && x !== "—",
    );
    return {
      id: r.id,
      modelName: r.modelName || r.modelId || "?",
      runtimeText: runtimePieces.length ? runtimePieces.join(" · ") : "—",
      latencyText: m.latencyMs ? m.latencyMs.toFixed(1) + "ms" : "—",
      memoryText: m.memoryMB ? m.memoryMB.toFixed(1) + "MB" : "—",
      sizeText: m.modelSizeMB ? m.modelSizeMB.toFixed(0) + "MB" : "—",
      avgConfText: conf == null ? "—" : (conf * 100).toFixed(1) + "%",
      userScoreText: us == null ? "—" : us > 0 ? "👍" : us < 0 ? "👎" : "🙂",
    };
  }),
);

// ---------------------------------------------------------------------
// Radar 雷达图（4 维：Speed / Memory / Size / UserScore） — 对齐 V1:179-277
// ---------------------------------------------------------------------
const radarSize = 360;
const radarCenter = 180;
const radarMaxR = 140;

const RADAR_AXES = [
  { key: "appBuilder.compare.radarAxisSpeed", fallback: "Speed" },
  { key: "appBuilder.compare.radarAxisMemory", fallback: "Memory" },
  { key: "appBuilder.compare.radarAxisSize", fallback: "Size" },
  { key: "appBuilder.compare.radarAxisUserScore", fallback: "Score" },
];
const RADAR_PALETTE = [
  "#a594ff", "#48bb78", "#ed8936", "#63b3ed",
  "#f56565", "#9f7aea", "#38b2ac", "#ecc94b",
];

interface RadarVertex {
  x: number;
  y: number;
}
interface RadarPolygon {
  id: string;
  modelName: string;
  vertices: RadarVertex[];
  points: string;
  strokeColor: string;
  color: string;
}

const radarPolygons = computed<RadarPolygon[]>(() => {
  const items = props.items;
  if (!items.length) return [];
  const all = items.map((r) => ({
    run: r,
    m: extractMetrics(r),
    us: userScore(r),
  }));
  const maxLat = Math.max(...all.map((x) => x.m.latencyMs)) || 1;
  const maxMem = Math.max(...all.map((x) => x.m.memoryMB)) || 1;
  const maxSize = Math.max(...all.map((x) => x.m.modelSizeMB)) || 1;
  return all.map((x, idx) => {
    const speed = x.m.latencyMs ? 1 - x.m.latencyMs / maxLat : 0.5;
    const mem = x.m.memoryMB ? 1 - x.m.memoryMB / maxMem : 0.5;
    const sz = x.m.modelSizeMB ? 1 - x.m.modelSizeMB / maxSize : 0.5;
    const us = x.us == null ? 0.05 : (x.us + 1) / 2; // -1..1 -> 0..1
    const values = [speed, mem, sz, us].map((v) =>
      Math.max(0, Math.min(1, v)),
    );
    const vertices = values.map((v, i) => {
      const angle = -Math.PI / 2 + i * (Math.PI / 2);
      return {
        x: radarCenter + Math.cos(angle) * radarMaxR * v,
        y: radarCenter + Math.sin(angle) * radarMaxR * v,
      };
    });
    const stroke =
      RADAR_PALETTE[idx % RADAR_PALETTE.length] ?? "#a594ff";
    return {
      id: x.run.id,
      modelName: x.run.modelName || x.run.modelId || "?",
      vertices,
      points: vertices.map((p) => p.x + "," + p.y).join(" "),
      strokeColor: stroke,
      color: stroke + "33", // alpha 0x33 ≈ 0.2
    };
  });
});

const radarRings = computed<string[]>(() => {
  const rings: string[] = [];
  for (let k = 1; k <= 4; k++) {
    const r = (radarMaxR * k) / 4;
    const pts: string[] = [];
    for (let i = 0; i < 4; i++) {
      const a = -Math.PI / 2 + i * (Math.PI / 2);
      pts.push(
        radarCenter + Math.cos(a) * r + "," + (radarCenter + Math.sin(a) * r),
      );
    }
    rings.push(pts.join(" "));
  }
  return rings;
});

const radarAxes = computed<RadarVertex[]>(() => {
  const out: RadarVertex[] = [];
  for (let i = 0; i < 4; i++) {
    const a = -Math.PI / 2 + i * (Math.PI / 2);
    out.push({
      x: radarCenter + Math.cos(a) * radarMaxR,
      y: radarCenter + Math.sin(a) * radarMaxR,
    });
  }
  return out;
});

const radarLabels = computed(() => {
  // 上 / 右 / 下 / 左 文本偏移
  const offsets = [
    { dx: 0, dy: -8, anchor: "middle" },
    { dx: 12, dy: 4, anchor: "start" },
    { dx: 0, dy: 18, anchor: "middle" },
    { dx: -12, dy: 4, anchor: "end" },
  ] as const;
  const fallbackOffset = { dx: 0, dy: -8, anchor: "middle" } as const;
  return RADAR_AXES.map((axis, i) => {
    const a = -Math.PI / 2 + i * (Math.PI / 2);
    const tipX = radarCenter + Math.cos(a) * radarMaxR;
    const tipY = radarCenter + Math.sin(a) * radarMaxR;
    const o = offsets[i] ?? fallbackOffset;
    return {
      textKey: axis.key,
      fallback: axis.fallback,
      x: tipX + o.dx,
      y: tipY + o.dy,
      anchor: o.anchor,
    };
  });
});
</script>

<template>
  <div
    v-if="isOpen"
    class="ab-compare-tray"
  >
    <div class="ab-compare-header">
      <span>{{ t("appBuilder.compare.title") }} ({{ count }})</span>
      <div
        class="ab-compare-mode-switch"
        role="tablist"
      >
        <button
          type="button"
          role="tab"
          :aria-selected="viewMode === 'cards'"
          :class="{ active: viewMode === 'cards' }"
          @click="viewMode = 'cards'"
        >
          {{ t("appBuilder.compare.viewCards") }}
        </button>
        <button
          type="button"
          role="tab"
          :aria-selected="viewMode === 'table'"
          :class="{ active: viewMode === 'table' }"
          @click="viewMode = 'table'"
        >
          {{ t("appBuilder.compare.viewTable") }}
        </button>
        <button
          type="button"
          role="tab"
          :aria-selected="viewMode === 'radar'"
          :class="{ active: viewMode === 'radar' }"
          @click="viewMode = 'radar'"
        >
          {{ t("appBuilder.compare.viewRadar") }}
        </button>
      </div>
      <button
        v-if="count"
        type="button"
        class="ab-compare-clear"
        @click="emit('clear')"
      >
        {{ t("appBuilder.compare.clear") }}
      </button>
      <button
        type="button"
        class="ab-compare-close"
        @click="emit('close')"
      >
        ×
      </button>
    </div>

    <div
      v-if="count === 0"
      class="ab-compare-empty"
    >
      {{ t("appBuilder.compare.empty") }}
    </div>

    <template v-else>
      <!-- Cards 视图 — 对齐 V1:326-351 -->
      <div
        v-if="viewMode === 'cards'"
        class="ab-compare-grid"
      >
        <div
          v-for="item in items"
          :key="item.id"
          class="ab-compare-card"
        >
          <div class="ab-compare-card-header">
            <span class="ab-compare-model">{{ item.modelName || item.modelId }}</span>
            <button
              type="button"
              class="ab-compare-remove"
              :title="t('appBuilder.compare.remove')"
              @click="emit('remove-run', item.id)"
            >
              ×
            </button>
          </div>
          <div class="ab-compare-card-body">
            <div class="ab-compare-metric">
              <strong>{{ t("appBuilder.compare.latency") }}:</strong>
              {{ (item.metrics && item.metrics.latencyMs) || "—" }}ms
            </div>
            <div
              v-if="item.metrics && item.metrics.memoryMB"
              class="ab-compare-metric"
            >
              <strong>{{ t("appBuilder.compare.memory") }}:</strong>
              {{ item.metrics.memoryMB }}MB
            </div>
            <div
              v-if="item.status"
              class="ab-compare-status"
            >
              {{ item.status === "completed" ? "✅" : "❌" }} {{ item.status }}
            </div>
            <div
              v-if="item.output"
              class="ab-compare-output"
            >
              <pre>{{ previewOutput(item.output) }}</pre>
            </div>
          </div>
        </div>
      </div>

      <!-- Table 视图 — 对齐 V1:353-385 -->
      <div
        v-else-if="viewMode === 'table'"
        class="ab-compare-table-wrap"
      >
        <table class="ab-compare-table">
          <thead>
            <tr>
              <th>{{ t("appBuilder.compare.colModel") }}</th>
              <th>{{ t("appBuilder.compare.colRuntime") }}</th>
              <th class="num">
                {{ t("appBuilder.compare.colLatency") }}
              </th>
              <th class="num">
                {{ t("appBuilder.compare.colMemory") }}
              </th>
              <th class="num">
                {{ t("appBuilder.compare.colSize") }}
              </th>
              <th class="num">
                {{ t("appBuilder.compare.colAvgConf") }}
              </th>
              <th class="num">
                {{ t("appBuilder.compare.colUserScore") }}
              </th>
              <th />
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in tableRows"
              :key="row.id"
              class="ab-compare-table-row"
            >
              <td
                class="ab-compare-table-name"
                :title="row.id"
              >
                {{ row.modelName }}
              </td>
              <td>{{ row.runtimeText }}</td>
              <td class="num">
                {{ row.latencyText }}
              </td>
              <td class="num">
                {{ row.memoryText }}
              </td>
              <td class="num">
                {{ row.sizeText }}
              </td>
              <td class="num">
                {{ row.avgConfText }}
              </td>
              <td class="num">
                {{ row.userScoreText }}
              </td>
              <td>
                <button
                  type="button"
                  class="ab-compare-remove"
                  :title="t('appBuilder.compare.remove')"
                  @click="emit('remove-run', row.id)"
                >
                  ×
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Radar 视图 — 对齐 V1:387-433 -->
      <div
        v-else-if="viewMode === 'radar'"
        class="ab-compare-radar-wrap"
      >
        <svg
          :viewBox="'0 0 ' + radarSize + ' ' + radarSize"
          class="ab-compare-radar-svg"
          :width="radarSize"
          :height="radarSize"
          aria-hidden="true"
        >
          <!-- 背景同心多边形 -->
          <polygon
            v-for="(ring, i) in radarRings"
            :key="'r' + i"
            :points="ring"
            class="ab-radar-ring"
          />
          <!-- 4 条轴线 -->
          <line
            v-for="(ax, i) in radarAxes"
            :key="'a' + i"
            :x1="radarCenter"
            :y1="radarCenter"
            :x2="ax.x"
            :y2="ax.y"
            class="ab-radar-axis"
          />
          <!-- 每个 run 一个多边形 -->
          <g
            v-for="poly in radarPolygons"
            :key="poly.id"
          >
            <polygon
              :points="poly.points"
              :fill="poly.color"
              :stroke="poly.strokeColor"
              class="ab-radar-poly"
            />
            <circle
              v-for="(p, j) in poly.vertices"
              :key="j"
              :cx="p.x"
              :cy="p.y"
              :r="3"
              :fill="poly.strokeColor"
            />
          </g>
          <!-- 轴标签 -->
          <text
            v-for="(lbl, i) in radarLabels"
            :key="'l' + i"
            :x="lbl.x"
            :y="lbl.y"
            :text-anchor="lbl.anchor"
            class="ab-radar-label"
          >{{ t(lbl.textKey) }}</text>
        </svg>
        <ul class="ab-compare-radar-legend">
          <li
            v-for="poly in radarPolygons"
            :key="poly.id"
          >
            <span
              class="ab-radar-legend-swatch"
              :style="{ background: poly.strokeColor }"
            />
            <span
              class="ab-radar-legend-text"
              :title="poly.id"
            >{{ poly.modelName }}</span>
            <button
              type="button"
              class="ab-compare-remove"
              :title="t('appBuilder.compare.remove')"
              @click="emit('remove-run', poly.id)"
            >
              ×
            </button>
          </li>
        </ul>
      </div>
    </template>
  </div>
</template>
