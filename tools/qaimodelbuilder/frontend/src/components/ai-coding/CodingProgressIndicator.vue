<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CodingProgressIndicator — live progress row for an in-flight coding turn
 * (V1 AiCodingPanel.js:1095-1121 parity).
 *
 * Shows a spinner + stage icon + "Processing" + elapsed seconds + an
 * Interrupt button + optional detail / tool name. Driven by the shared
 * `useCodingSession` `sessionProgress` state (stage / detail / toolName /
 * startTime). Rendered only while a stream is active for the session.
 */
import { computed, onBeforeUnmount, ref } from "vue";
import { useI18n } from "vue-i18n";
import type { SessionProgress } from "@/composables/useCodingSession";

const props = defineProps<{ progress: SessionProgress }>();
const emit = defineEmits<{ interrupt: [] }>();

const { t } = useI18n();

// Re-render the elapsed counter once per second (V1 ticks via Date.now()).
const now = ref(Date.now());
const timer = window.setInterval(() => {
  now.value = Date.now();
}, 1000);
onBeforeUnmount(() => window.clearInterval(timer));

const elapsedSec = computed<number>(() =>
  Math.max(0, Math.floor((now.value - props.progress.startTime) / 1000)),
);

// V1 AiCodingPanel.js:160-171 stage → icon.
const stageIcon = computed<string>(() => {
  switch (props.progress.stage) {
    case "start":
      return "🔄";
    case "submitted":
      return "📤";
    case "tool_start":
      return "⚙️";
    case "tool_done":
      return "✅";
    case "retry":
      return "🔁";
    case "error":
      return "❌";
    case "awaiting_approval":
      return "⏳";
    default:
      return "⏳";
  }
});
</script>

<template>
  <div
    class="cc-progress"
    data-testid="cc-progress"
  >
    <span
      class="spinner cc-progress__spinner"
      aria-hidden="true"
    ></span>
    <span aria-hidden="true">{{ stageIcon }}</span>
    <span class="cc-progress__label">{{ t("aiCoding.panel.processing") }}</span>
    <span class="cc-progress__elapsed">{{ elapsedSec }}s</span>
    <button
      type="button"
      class="cc-progress__stop"
      :title="t('aiCoding.panel.stopTaskTitle')"
      data-testid="cc-progress-interrupt"
      @click="emit('interrupt')"
    >
      ⏹ {{ t("claudeCode.stopTask") }}
    </button>
    <span
      v-if="progress.detail"
      class="cc-progress__detail"
    >{{ progress.detail }}</span>
    <span
      v-if="progress.toolName"
      class="cc-progress__tool"
    >🔧 {{ progress.toolName }}</span>
  </div>
</template>

<style scoped>
.cc-progress {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #9b97c4);
  background: var(--bg-secondary, #161430);
  border: 1px solid var(--border, #2a2750);
  border-radius: 8px;
  flex-wrap: wrap;
}
.cc-progress__spinner {
  width: 12px;
  height: 12px;
  border-width: 1.5px;
  flex-shrink: 0;
}
.cc-progress__label {
  font-weight: 600;
}
.cc-progress__elapsed {
  font-variant-numeric: tabular-nums;
}
.cc-progress__stop {
  border: 1px solid rgba(248, 113, 113, 0.4);
  background: transparent;
  color: #f87171;
  border-radius: 6px;
  padding: 1px 8px;
  cursor: pointer;
  font-size: var(--text-xs, 11px);
}
.cc-progress__detail {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cc-progress__tool {
  flex-shrink: 0;
}
</style>
