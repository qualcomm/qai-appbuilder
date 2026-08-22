<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
 * StepProgressCard.vue — MB Pro step execution progress card.
 *
 * Rendered inside the assistant message's tool-call area (ChatMessageList →
 * ToolCallList) in place of the generic ``ToolExecPanel`` whenever
 * ``call.toolName === "show_step_progress"``.
 *
 * Payload contract (upstream result parsed by ``_parse_step_progress`` in
 * ``mb_pro_mapper.py``):
 *   call.result = JSON string:
 *   {
 *     kind: "step_progress",
 *     focus: string,
 *     steps: [
 *       { n: number, title: string, deps: string, status: "done"|"doing"|"todo"|"failed" }
 *     ]
 *   }
 *
 * Falls back gracefully to a <pre> block when result is not parseable.
-->
<script setup lang="ts">
import { computed } from "vue";

interface Step {
  n: number;
  title: string;
  deps: string;
  status: "done" | "doing" | "todo" | "failed";
}

interface StepProgressPayload {
  kind: "step_progress";
  focus: string;
  steps: Step[];
}

interface Props {
  result?: string;
}

const props = withDefaults(defineProps<Props>(), { result: "" });

const parsed = computed<StepProgressPayload | null>(() => {
  if (!props.result) return null;
  try {
    const obj = JSON.parse(props.result) as unknown;
    if (
      obj !== null &&
      typeof obj === "object" &&
      (obj as Record<string, unknown>).kind === "step_progress"
    ) {
      return obj as StepProgressPayload;
    }
  } catch {
    /* ignore */
  }
  return null;
});

const steps = computed<Step[]>(() => parsed.value?.steps ?? []);
const focus = computed<string>(() => parsed.value?.focus ?? "");

const total = computed(() => steps.value.length);
const doneCount = computed(() => steps.value.filter((s) => s.status === "done").length);
const progressPct = computed(() =>
  total.value > 0 ? Math.round((doneCount.value / total.value) * 100) : 0,
);

function statusIcon(status: Step["status"]): string {
  return { done: "✅", doing: "▶", failed: "❌", todo: "⬚" }[status] ?? "⬚";
}
</script>

<template>
  <section class="step-progress-card" data-testid="step-progress-card">
    <header class="step-progress-card__header">
      <span class="step-progress-card__icon" aria-hidden="true">📋</span>
      <span class="step-progress-card__title">执行进度</span>
      <span class="step-progress-card__counter">{{ doneCount }} / {{ total }}</span>
    </header>

    <div
      class="step-progress-card__bar-track"
      role="progressbar"
      :aria-valuenow="progressPct"
      aria-valuemin="0"
      aria-valuemax="100"
    >
      <div
        class="step-progress-card__bar-fill"
        :style="{ width: progressPct + '%' }"
      />
    </div>

    <div v-if="focus" class="step-progress-card__focus">
      {{ focus }}
    </div>

    <pre v-if="!parsed" class="step-progress-card__raw">{{ result }}</pre>

    <table v-else class="step-progress-card__table">
      <thead>
        <tr>
          <th class="step-progress-card__th step-progress-card__th--n">#</th>
          <th class="step-progress-card__th">步骤</th>
          <th class="step-progress-card__th step-progress-card__th--deps">依赖</th>
          <th class="step-progress-card__th step-progress-card__th--status">状态</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="step in steps"
          :key="step.n"
          class="step-progress-card__row"
          :class="`step-progress-card__row--${step.status}`"
        >
          <td class="step-progress-card__td step-progress-card__td--n">{{ step.n }}</td>
          <td class="step-progress-card__td">{{ step.title }}</td>
          <td class="step-progress-card__td step-progress-card__td--deps">{{ step.deps }}</td>
          <td class="step-progress-card__td step-progress-card__td--status">
            <span class="step-progress-card__status-icon" aria-hidden="true">
              {{ statusIcon(step.status) }}
            </span>
            <span class="step-progress-card__status-text">{{ step.status }}</span>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<style scoped>
.step-progress-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  margin: 6px 0;
  border-radius: 8px;
  border: 1px solid var(--color-border, rgba(0, 0, 0, 0.12));
  background: var(--color-surface, rgba(255, 255, 255, 0.6));
}

.step-progress-card__header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
}

.step-progress-card__icon {
  font-size: 16px;
  line-height: 1;
}

.step-progress-card__title {
  flex: 1;
  color: var(--color-text, inherit);
}

.step-progress-card__counter {
  font-size: 12px;
  font-weight: 500;
  color: var(--color-text-muted, #6b7280);
  font-variant-numeric: tabular-nums;
}

.step-progress-card__bar-track {
  height: 5px;
  border-radius: 999px;
  background: var(--color-border, rgba(0, 0, 0, 0.1));
  overflow: hidden;
}

.step-progress-card__bar-fill {
  height: 100%;
  border-radius: 999px;
  background: var(--color-primary, #4f46e5);
  transition: width 0.3s ease;
}

.step-progress-card__focus {
  font-size: 12px;
  color: var(--color-text-muted, #6b7280);
  padding: 4px 8px;
  border-radius: 4px;
  background: var(--color-surface, rgba(0, 0, 0, 0.03));
}

.step-progress-card__raw {
  font-size: 11px;
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
  max-height: 300px;
  overflow-y: auto;
  color: var(--color-text-muted, #6b7280);
}

.step-progress-card__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.step-progress-card__th {
  text-align: left;
  padding: 4px 8px;
  font-weight: 600;
  font-size: 11px;
  color: var(--color-text-muted, #6b7280);
  border-bottom: 1px solid var(--color-border, rgba(0, 0, 0, 0.08));
  white-space: nowrap;
}

.step-progress-card__th--n,
.step-progress-card__td--n {
  width: 28px;
  text-align: center;
}

.step-progress-card__th--deps,
.step-progress-card__td--deps {
  width: 60px;
  text-align: center;
  color: var(--color-text-muted, #9ca3af);
}

.step-progress-card__th--status,
.step-progress-card__td--status {
  width: 90px;
  white-space: nowrap;
}

.step-progress-card__td {
  padding: 5px 8px;
  border-bottom: 1px solid var(--color-border, rgba(0, 0, 0, 0.04));
  vertical-align: middle;
  color: var(--color-text, inherit);
}

.step-progress-card__row--done .step-progress-card__td {
  color: var(--color-text-muted, #6b7280);
}

.step-progress-card__row--doing {
  background: rgba(79, 70, 229, 0.04);
}

.step-progress-card__row--doing .step-progress-card__td {
  font-weight: 500;
}

.step-progress-card__row--failed .step-progress-card__td {
  color: var(--color-error, #b91c1c);
}

.step-progress-card__status-icon {
  margin-right: 4px;
}

.step-progress-card__status-text {
  font-size: 11px;
  color: var(--color-text-muted, #6b7280);
}

.step-progress-card__row--doing .step-progress-card__status-text {
  color: var(--color-primary, #4f46e5);
}

.step-progress-card__row--failed .step-progress-card__status-text {
  color: var(--color-error, #b91c1c);
}
</style>
