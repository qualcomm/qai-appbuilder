<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CodingSubTaskList — V1-parity "🤖 sub-tasks" cards for Claude Code
 * Task/Agent sub-tasks (V1 `index.html:588-628`).
 *
 * Renders the sub-task list accumulated on an assistant message from the
 * `task_started` / `task_progress` / `task_notification` stream frames
 * (see `useCodingSession`). Each row shows a status glyph (running spinner
 * / ✓ / ✗ / ⏹), the description (falling back to a short task id), the
 * status label, an optional summary, and a usage rollup (tokens / tools /
 * duration). Presentational only — no business logic.
 *
 * Extracted as its own component (not inlined into ChatViewClaudeCode) so
 * the view stays within the cohesion budget and mirrors the existing
 * `CodingToolCallCard` factoring (judge 1: single responsibility).
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { CodingSubTask } from "@/composables/useCodingSession";

const props = defineProps<{ subTasks: CodingSubTask[] }>();

const { t } = useI18n();

const runningCount = computed(
  () => props.subTasks.filter((s) => s.status === "running").length,
);

function statusGlyph(status: CodingSubTask["status"]): string {
  switch (status) {
    case "completed":
      return "✓";
    case "failed":
      return "✗";
    case "stopped":
      return "⏹";
    default:
      return "";
  }
}

function statusLabel(status: CodingSubTask["status"]): string {
  switch (status) {
    case "completed":
      return t("index.statusDone");
    case "failed":
      return t("index.statusFailed");
    case "stopped":
      return t("index.statusStopped");
    default:
      return t("index.statusRunning");
  }
}

/** V1 fallback: when no description, show first 8 chars of the task id. */
function displayDesc(sub: CodingSubTask): string {
  return sub.description !== "" ? sub.description : sub.task_id.slice(0, 8);
}

/** V1 summary cap (index.html:618 — first 200 chars). */
function displaySummary(summary: string): string {
  return summary.length > 200 ? summary.slice(0, 200) : summary;
}
</script>

<template>
  <div
    v-if="subTasks.length > 0"
    class="cc-subtasks"
    data-testid="cc-subtasks"
  >
    <div class="cc-subtasks__header">
      <span>🤖 {{ t("index.subTasksHeader") }}</span>
      <span class="cc-subtasks__count">{{ subTasks.length }}</span>
      <span
        v-if="runningCount > 0"
        class="cc-subtasks__spinner"
        aria-hidden="true"
      ></span>
    </div>
    <div
      v-for="sub in subTasks"
      :key="sub.task_id"
      class="cc-subtask"
      :class="`cc-subtask--${sub.status}`"
    >
      <div class="cc-subtask__row">
        <span
          v-if="sub.status === 'running'"
          class="cc-subtask__spinner"
          aria-hidden="true"
        ></span>
        <span
          v-else
          class="cc-subtask__glyph"
          aria-hidden="true"
        >{{ statusGlyph(sub.status) }}</span>
        <span class="cc-subtask__desc">{{ displayDesc(sub) }}</span>
        <span class="cc-subtask__status">{{ statusLabel(sub.status) }}</span>
      </div>
      <div
        v-if="sub.summary !== ''"
        class="cc-subtask__summary"
      >
        {{ displaySummary(sub.summary) }}
      </div>
      <div
        v-if="sub.usage"
        class="cc-subtask__usage"
      >
        <span v-if="sub.usage.total_tokens !== undefined">🔢 {{ sub.usage.total_tokens }}</span>
        <span v-if="sub.usage.tool_uses !== undefined">🔧 {{ sub.usage.tool_uses }} {{ t("index.unitTools") }}</span>
        <span v-if="sub.usage.duration_ms !== undefined">⏱ {{ (sub.usage.duration_ms / 1000).toFixed(1) }}s</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* V1 parity (index.html:588-628): purple-framed sub-task list. */
.cc-subtasks {
  margin-top: var(--space-2);
  border: 1px solid var(--accent, #7c6cff);
  border-radius: var(--radius-md, 8px);
  background: var(--accent-soft, rgba(124, 108, 255, 0.08));
  padding: var(--space-2);
}

.cc-subtasks__header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--accent, #7c6cff);
  margin-bottom: var(--space-2);
}

.cc-subtasks__count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--accent, #7c6cff);
  color: #fff;
  font-size: var(--text-xs);
}

.cc-subtask {
  padding: var(--space-1) 0;
  border-top: 1px solid var(--border);
}

.cc-subtask:first-of-type {
  border-top: none;
}

.cc-subtask__row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-sm);
}

.cc-subtask__glyph {
  flex-shrink: 0;
  width: 16px;
  text-align: center;
}

.cc-subtask--completed .cc-subtask__glyph {
  color: var(--success, #4ade80);
}

.cc-subtask--failed .cc-subtask__glyph {
  color: var(--error, #f87171);
}

.cc-subtask--stopped .cc-subtask__glyph {
  color: var(--text-muted);
}

.cc-subtask__desc {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cc-subtask__status {
  flex-shrink: 0;
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.cc-subtask__summary {
  margin-top: 2px;
  padding-left: 24px;
  font-size: var(--text-xs);
  color: var(--text-secondary);
  line-height: 1.5;
}

.cc-subtask__usage {
  margin-top: 2px;
  padding-left: 24px;
  display: flex;
  gap: var(--space-2);
  font-size: var(--text-xs);
  color: var(--text-muted);
}

/* Spinners (running state) — reuse the rotating-ring pattern. */
.cc-subtasks__spinner,
.cc-subtask__spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  flex-shrink: 0;
  border: 2px solid var(--border);
  border-top-color: var(--accent, #7c6cff);
  border-radius: 50%;
  animation: cc-subtask-spin 0.8s linear infinite;
}

@keyframes cc-subtask-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
