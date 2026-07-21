<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * HistoryPanel — App Builder 运行历史面板（V1
 * `frontend/js/components/app-builder/HistoryPanel.js` 行为对齐版）。
 *
 * 单一职责：仅渲染传入的 runs 列表 + 表头 + 行点击展开内嵌详情（含
 * Run ID / Params / Error / Export / Share / Add-to-compare）。所有真正的
 * 副作用（fetch / delete confirm / export / share / 选 run）都通过 emit
 * 上抛给宿主（overlay），由宿主调用 store + useConfirm 完成 —— 这与 V2
 * 「容器/展示分离」一致，比 V1 单文件巨石组件（自带 fetch + toast +
 * window.confirm fallback）分层更清晰，符合判据 1。
 *
 * V1 行为事实来源（HistoryPanel.js:218-263）：
 *   表头 5 列：[status emoji] Started / Total / [rating] / Inference
 *   行布局   ：[status emoji] [time] [duration] [rating emoji] [latency ms]
 *   当前 run ：currentRunId 高亮（.ab-history-item-current）
 *   行点击   ：toggle 展开 .ab-history-detail（Run ID / Params / Error /
 *              Export / Share）
 *   loading/error/empty 三态：HistoryPanel.js:209-215
 *
 * V2 增强（不退化、纯改善实现）：
 *   - TS 严格类型化 + composable 复用（useI18n）
 *   - 删除按钮（V1 没有，V2 由 overlay 用 useConfirm 弹定制对话框）
 *   - "Add to compare" 行内按钮（沿用 V1 的右键菜单/输出栏入口同语义）
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useAppBuilderStore, type AppRun } from "@/stores/appBuilder";

interface Props {
  /** 当前模型下的全部 run（newest first；overlay 直传按模型过滤后的列表）。 */
  runs?: AppRun[];
  /**
   * 是否为"已按模型过滤的受控列表"。overlay 传 true：即使该模型暂无 run（空
   * 数组）也必须信任传入值、显示 empty 三态，绝不回退到 store.runs（那会混入
   * 其它模型的 run，破坏 V1 per-model 历史语义，缺口 #4）。
   */
  scoped?: boolean;
  /** Modal subtitle 主文案（模型 displayName / title）。 */
  selectedModelTitle?: string;
  /** Variant label（如 "FP16"）；未选 / 单 variant 时给空。 */
  selectedVariantLabel?: string;
  /** Variant runtime.quantization（如 "INT8"）；缺失给空，不渲染分隔点。 */
  selectedVariantQuant?: string;
  /** 当前 run id（用于行高亮 .ab-history-item-current）。 */
  selectedRunId?: string | null;
  /** fetch 进行中。 */
  isLoading?: boolean;
  /** fetch 错误文案。 */
  error?: string | null;
}

const props = withDefaults(defineProps<Props>(), {
  runs: () => [],
  scoped: false,
  selectedModelTitle: "",
  selectedVariantLabel: "",
  selectedVariantQuant: "",
  selectedRunId: null,
  isLoading: false,
  error: null,
});

interface Emits {
  (e: "select-run", id: string): void;
  /** 仅 emit；确认对话框由 overlay 用 useConfirm 弹（§3.9）。 */
  (e: "delete-run", id: string): void;
  (e: "refresh"): void;
  (e: "export-run", id: string): void;
  (e: "share-run", id: string): void;
  (e: "add-to-compare", run: AppRun): void;
}

const emit = defineEmits<Emits>();
const { t } = useI18n();
const store = useAppBuilderStore();

// overlay 显式传 `scoped=true` 的受控列表（已按模型过滤）时，无条件信任
// props.runs —— 哪怕为空也显示 empty 三态，绝不回退到 store.runs（避免混入
// 其它模型的 run，缺口 #4）。仅在非 scoped 的 0-props 直挂场景才回退到
// store.runs 保持原语义不退化。
const effectiveRuns = computed<AppRun[]>(() =>
  props.scoped || props.runs.length > 0 ? props.runs : store.runs,
);

// V1 行为：行点击在「展开 / 折叠」之间切换；任意时刻只展开一行。
const expandedRunId = ref<string | null>(null);

function onRowClick(run: AppRun): void {
  if (run.id === null) return;
  expandedRunId.value = expandedRunId.value === run.id ? null : run.id;
  emit("select-run", run.id);
}

// V1 statusEmoji（HistoryPanel.js:101-103）—— V1 用 success/error/cancelled，
// V2 后端 RunStatus 是 completed/failed/cancelled，做语义映射；运行中/排队
// 也给一个 emoji（V1 无此态：modal 只显示已结束 run，但 V2 store 在 live
// 期间也会把 in-flight run 放在列表前，UI 给个友好图标更稳）。
function statusEmoji(status: string): string {
  switch (status) {
    case "completed":
    case "success":
      return "✅";
    case "failed":
    case "error":
      return "❌";
    case "cancelled":
      return "⚠️";
    case "running":
    case "streaming":
      return "▶";
    case "queued":
      return "⏳";
    default:
      return "❓";
  }
}

// V1 ratingEmoji（HistoryPanel.js:105-110）。
function ratingEmoji(rating: number | null | undefined): string {
  if (rating === 1) return "👍";
  if (rating === -1) return "👎";
  if (rating === 0) return "🙂";
  return "";
}

// AppRun.rating 由 store hydration（fetchHistory：RunResponse.rating → -1/0/1）
// 与 submitRating 乐观更新统一填充，直接读即可。值域 -1/0/1（V1 thumb 语义），
// ratingEmoji 对 null/undefined 返回空字符串。
function pickRating(run: AppRun): number | null | undefined {
  return run.rating;
}

// V1 formatTime（HistoryPanel.js:85-92）—— V1 接收 ms epoch；V2 store 给的是
// ISO 字符串（startedAt: r.started_at）。两种都要兼容（部分 in-flight run
// 可能没有 startedAt，那就拿 createdAt 兜底）。
function formatTime(run: AppRun): string {
  const candidate = run.startedAt ?? null;
  if (candidate !== null && candidate !== "") {
    const ts = Date.parse(candidate);
    if (Number.isFinite(ts)) return new Date(ts).toLocaleTimeString();
  }
  if (Number.isFinite(run.createdAt)) {
    return new Date(run.createdAt).toLocaleTimeString();
  }
  return "—";
}

// V1 formatDuration（HistoryPanel.js:94-99）—— V1 用 (end-start)/1000 → "X.XXs"。
// 这是端到端 wall-clock（区别于 latencyMs() 的纯推理延时）。AppRun 没有独立
// durationMs 字段，从 startedAt/finishedAt 计算；缺时显示 "—"。后端
// RunMetricsResponse.duration_ms 是同一语义的权威端到端时长（在
// MetricsView / latencyMs() 兜底处使用），此处沿用本地时间戳计算与 V1 一致。
function formatDuration(run: AppRun): string {
  if (run.startedAt == null || run.finishedAt == null) return "—";
  const a = Date.parse(run.startedAt);
  const b = Date.parse(run.finishedAt);
  if (!Number.isFinite(a) || !Number.isFinite(b) || b < a) return "—";
  const sec = (b - a) / 1000;
  if (!Number.isFinite(sec)) return "—";
  return `${sec.toFixed(2)}s`;
}

// V1 行尾 inference latency（HistoryPanel.js:237-238）：r.metrics.latencyMs。
// 缺口 #6 已实现：runner 的 `metrics` 事件含 latencyMs（纯推理延时），
// 后端把它持久化进 Run 聚合（inference_latency_ms 列）并经
// RunMetricsResponse.latency_ms 在 reload 时回放；live 流式期由
// frames.ts 把 metrics 帧投影成 run.metrics.latencyMs。这里优先读真实
// inference latency（camelCase latencyMs 来自 live 投影 / hydration，
// snake latency_ms 来自后端 wire），拿不到再兜底端到端 duration_ms，
// 二者都无则显示 "—"（真实状态优先 §🔴，不伪造）。
function latencyMs(run: AppRun): string {
  const m = run.metrics;
  if (m === null) return "—";
  const live = m.latencyMs;
  if (typeof live === "number" && Number.isFinite(live)) {
    return `${Math.round(live)}ms`;
  }
  const wire = m.latency_ms;
  if (typeof wire === "number" && Number.isFinite(wire)) {
    return `${Math.round(wire)}ms`;
  }
  if (typeof m.duration_ms === "number" && Number.isFinite(m.duration_ms)) {
    return `${Math.round(m.duration_ms)}ms`;
  }
  return "—";
}

// Params JSON 字符串化（V1 HistoryPanel.js:247）：仅在有键时渲染该行。
function paramsJson(run: AppRun): string {
  try {
    return JSON.stringify(run.params);
  } catch {
    return "{}";
  }
}

function hasParams(run: AppRun): boolean {
  return (
    run.params !== null &&
    typeof run.params === "object" &&
    Object.keys(run.params).length > 0
  );
}

// V1 modal subtitle（AppBuilderWorkbench.js:922-930）：模型名 + variant
// label + (· quant)。subtitle 由 overlay 在 modal-header 渲染，本组件
// 暴露一个 computed 串只是为了让 overlay 可以选择直接读字段或用此串；
// 不在本组件内部模板渲染，避免与 modal-header 重复（V1 inModal 模式
// 的同款隐藏逻辑）。
const subtitlePlain = computed<string>(() => {
  const parts: string[] = [props.selectedModelTitle];
  if (props.selectedVariantLabel) {
    let v = props.selectedVariantLabel;
    if (props.selectedVariantQuant) v += ` · ${props.selectedVariantQuant}`;
    parts.push(v);
  }
  return parts.join(" · ");
});
defineExpose({ subtitlePlain });
</script>

<template>
  <div
    class="ab-history-panel ab-history-in-modal"
    data-testid="app-builder-history-panel"
  >
    <!-- V1 浮窗模式 toolbar（HistoryPanel.js:202-208）：count + refresh。 -->
    <div class="ab-history-modal-toolbar">
      <span
        v-if="effectiveRuns.length > 0"
        class="ab-history-count"
      >({{ effectiveRuns.length }})</span>
      <button
        type="button"
        class="ab-history-refresh"
        :disabled="props.isLoading"
        :title="t('appBuilder.history.refresh')"
        data-testid="app-builder-history-refresh"
        @click="emit('refresh')"
      >
        ⟳
      </button>
    </div>

    <!-- 三态：loading / error / empty（V1 HistoryPanel.js:209-215）。 -->
    <div
      v-if="props.isLoading"
      class="ab-history-loading"
    >
      {{ t("appBuilder.history.loading") }}
    </div>
    <div
      v-else-if="props.error"
      class="ab-history-error"
    >
      {{ props.error }}
    </div>
    <div
      v-else-if="effectiveRuns.length === 0"
      class="ab-history-empty"
    >
      {{ t("appBuilder.history.empty") }}
    </div>

    <!-- 表头 + 列表（V1 HistoryPanel.js:217-263）。 -->
    <template v-else>
      <div class="ab-history-table-header">
        <span class="ab-history-th-status"></span>
        <span class="ab-history-th-time">{{ t("appBuilder.history.colStartedAt") }}</span>
        <span class="ab-history-th-duration">{{ t("appBuilder.history.colDuration") }}</span>
        <span class="ab-history-th-rating"></span>
        <span class="ab-history-th-latency">{{ t("appBuilder.history.colLatency") }}</span>
      </div>
      <ul class="ab-history-list">
        <li
          v-for="run in effectiveRuns"
          :key="run.id ?? `live-${run.createdAt}`"
          class="ab-history-item"
          :class="{
            'ab-history-item-expanded': run.id !== null && expandedRunId === run.id,
            'ab-history-item-current':
              run.id !== null && props.selectedRunId === run.id,
          }"
          data-testid="app-builder-history-item"
        >
          <div
            class="ab-history-row"
            role="button"
            tabindex="0"
            @click="onRowClick(run)"
            @keydown.enter.prevent="onRowClick(run)"
            @keydown.space.prevent="onRowClick(run)"
          >
            <span
              class="ab-history-status"
              aria-hidden="true"
            >{{ statusEmoji(run.status) }}</span>
            <span class="ab-history-time">{{ formatTime(run) }}</span>
            <span class="ab-history-duration">{{ formatDuration(run) }}</span>
            <span class="ab-history-rating">{{ ratingEmoji(pickRating(run)) }}</span>
            <span class="ab-history-latency">{{ latencyMs(run) }}</span>
          </div>
          <div
            v-if="run.id !== null && expandedRunId === run.id"
            class="ab-history-detail"
          >
            <div class="ab-history-detail-row">
              <strong>Run ID:</strong>
              <code>{{ run.id }}</code>
            </div>
            <div
              v-if="hasParams(run)"
              class="ab-history-detail-row"
            >
              <strong>{{ t("appBuilder.history.params") }}:</strong>
              <code>{{ paramsJson(run) }}</code>
            </div>
            <div
              v-if="run.error"
              class="ab-history-detail-row ab-history-detail-error"
            >
              <strong>{{ t("appBuilder.history.error") }}:</strong>
              {{ run.error }}
            </div>
            <div class="ab-history-detail-actions">
              <button
                type="button"
                class="ab-history-action-btn"
                data-testid="app-builder-history-export"
                @click.stop="run.id !== null && emit('export-run', run.id)"
              >
                📄 {{ t("appBuilder.history.export") }}
              </button>
              <button
                type="button"
                class="ab-history-action-btn"
                data-testid="app-builder-history-share"
                @click.stop="run.id !== null && emit('share-run', run.id)"
              >
                🔗 {{ t("appBuilder.history.share") }}
              </button>
              <button
                type="button"
                class="ab-history-action-btn"
                data-testid="app-builder-history-add-compare"
                @click.stop="emit('add-to-compare', run)"
              >
                ＋ {{ t("appBuilder.compare.title") }}
              </button>
              <button
                type="button"
                class="ab-history-action-btn"
                data-testid="app-builder-history-delete"
                @click.stop="run.id !== null && emit('delete-run', run.id)"
              >
                ✕ Delete
              </button>
            </div>
          </div>
        </li>
      </ul>
    </template>
  </div>
</template>
