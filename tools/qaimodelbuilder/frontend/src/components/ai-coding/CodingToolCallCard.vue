<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CodingToolCallCard — renders a single tool call inside a coding
 * assistant message (功能块 6/7, V1 `AiCodingPanel.js` tool-card parity).
 *
 * Shows the tool name + a friendly summary of its arguments, a status
 * badge (running / done / error), and a collapsible region for the raw
 * args + result/output. Pairs `tool_call` ⇄ `tool_result` by id upstream
 * (see useCodingSession); this component only renders the merged state.
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import type { CodingToolCall } from "@/composables/useCodingSession";
import { iconForTool } from "@/utils/codingToolIcons";

const props = defineProps<{ call: CodingToolCall }>();
const { t } = useI18n();

const expanded = ref(false);

const statusLabel = computed<string>(() => {
  switch (props.call.status) {
    case "running":
      return t("claudeCode.toolStatusRunning");
    case "error":
      return t("claudeCode.toolStatusError");
    default:
      return t("claudeCode.toolStatusDone");
  }
});

/** A compact one-line summary of the most informative argument. */
const argSummary = computed<string>(() => {
  const a = props.call.args ?? {};
  const candidates = [
    a["command"],
    a["cmd"],
    a["path"],
    a["file_path"],
    a["pattern"],
    a["query"],
    a["url"],
  ];
  for (const c of candidates) {
    if (typeof c === "string" && c !== "") {
      return c.length > 80 ? `${c.slice(0, 80)}…` : c;
    }
  }
  return "";
});

const argsJson = computed<string>(() => {
  try {
    return JSON.stringify(props.call.args ?? {}, null, 2);
  } catch {
    return String(props.call.args);
  }
});

/**
 * V1-parity tool icon (shared registry — see utils/codingToolIcons.ts).
 * 13 named entries replace the previous 7 substring heuristics so e.g.
 * `Task` renders as 🎯 instead of falling through to 🤖, and `LS` renders
 * as 📂 distinct from `Glob` 📁.
 */
const toolIcon = computed<string>(() => iconForTool(props.call.tool));
</script>

<template>
  <div
    class="cc-tool-card"
    :class="`cc-tool-card--${call.status}`"
    :data-testid="`cc-tool-card-${call.id}`"
  >
    <button
      type="button"
      class="cc-tool-card__head"
      :data-testid="`cc-tool-card-toggle-${call.id}`"
      @click="expanded = !expanded"
    >
      <span
        class="cc-tool-card__icon"
        aria-hidden="true"
      >{{ toolIcon }}</span>
      <span class="cc-tool-card__name">{{ call.tool }}</span>
      <span
        v-if="argSummary !== ''"
        class="cc-tool-card__summary"
      >{{ argSummary }}</span>
      <span
        class="cc-tool-card__status"
        :class="`cc-tool-card__status--${call.status}`"
        :data-testid="`cc-tool-card-status-${call.id}`"
      >
        <span
          v-if="call.status === 'running'"
          class="cc-tool-card__spinner"
          aria-hidden="true"
        ></span>
        {{ statusLabel }}
      </span>
      <span class="cc-tool-card__chevron">{{ expanded ? "▾" : "▸" }}</span>
    </button>

    <div
      v-if="expanded"
      class="cc-tool-card__body"
      :data-testid="`cc-tool-card-body-${call.id}`"
    >
      <div class="cc-tool-card__section">
        <span class="cc-tool-card__label">args</span>
        <pre class="cc-tool-card__pre">{{ argsJson }}</pre>
      </div>
      <div
        v-if="call.output !== undefined && call.output !== ''"
        class="cc-tool-card__section"
      >
        <span class="cc-tool-card__label">{{
          call.isError ? "error" : "result"
        }}</span>
        <pre
          class="cc-tool-card__pre"
          :class="{ 'cc-tool-card__pre--error': call.isError }"
        >{{ call.output }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cc-tool-card {
  border: 1px solid var(--border, #2a2750);
  border-radius: 6px;
  background: var(--bg-primary, #0f0d24);
  overflow: hidden;
}
.cc-tool-card--running {
  border-color: var(--accent, #7c6cf0);
}
.cc-tool-card--error {
  border-color: var(--error, #ef4444);
}
.cc-tool-card__head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 10px;
  background: transparent;
  border: none;
  color: var(--text-primary, #e9e7ff);
  cursor: pointer;
  text-align: left;
  font-size: var(--text-sm, 13px);
}
.cc-tool-card__icon {
  flex: none;
}
.cc-tool-card__name {
  font-weight: 600;
  flex: none;
}
.cc-tool-card__summary {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-muted, #9b97c4);
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs, 11px);
}
.cc-tool-card__status {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--text-xs, 11px);
  padding: 1px 6px;
  border-radius: 10px;
}
.cc-tool-card__status--running {
  color: var(--accent, #a594ff);
}
.cc-tool-card__status--done {
  color: var(--success, #34d399);
}
.cc-tool-card__status--error {
  color: var(--error, #ef4444);
}
.cc-tool-card__spinner {
  width: 8px;
  height: 8px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  display: inline-block;
  animation: cc-tool-spin 0.7s linear infinite;
}
@keyframes cc-tool-spin {
  to {
    transform: rotate(360deg);
  }
}
.cc-tool-card__chevron {
  flex: none;
  color: var(--text-muted, #6b7280);
}
.cc-tool-card__body {
  padding: 6px 10px;
  border-top: 1px solid var(--border, #2a2750);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.cc-tool-card__label {
  font-size: var(--text-xs, 11px);
  text-transform: uppercase;
  color: var(--text-muted, #6b7280);
}
.cc-tool-card__pre {
  margin: 2px 0 0;
  padding: 6px 8px;
  background: var(--bg-secondary, #161430);
  border-radius: 4px;
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs, 11px);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 240px;
  overflow: auto;
}
.cc-tool-card__pre--error {
  color: var(--error, #ef4444);
}
</style>
