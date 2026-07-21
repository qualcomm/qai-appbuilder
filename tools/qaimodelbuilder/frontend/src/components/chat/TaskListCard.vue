<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * TaskListCard — an in-conversation snapshot of a `todowrite` tool call
 * (V2 enhancement; V1 has no equivalent).
 *
 * Rendered inside the assistant message's tool-call area (ChatMessageList) in
 * place of the generic ToolExecPanel whenever `call.tool === "todowrite"`. It
 * shows the task list the model wrote AT THAT POINT — a historical snapshot
 * that scrolls with the conversation (each `todowrite` call = one card). The
 * data comes straight from the persisted tool-call `args.todos`, so a history
 * reload re-renders it with zero extra persistence (the tool_calls JSON is
 * already saved + rehydrated by historyMapper).
 *
 * Default EXPANDED (user requirement: "shown in the chat window default
 * expanded"); a header click collapses it to just the progress line.
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import type { TodoItem } from "@/stores/_chatTabsTypes";

const props = defineProps<{
  /** Raw `arguments` of the todowrite tool call (`{ todos: [...] }`). */
  args: Record<string, unknown>;
}>();

const { t } = useI18n();

const VALID_STATUS = new Set([
  "pending",
  "in_progress",
  "completed",
  "cancelled",
]);

/** Parse + sanitise the todos out of the persisted tool-call args. Mirrors
 *  the store's `applyTodoWrite` validation so live + reloaded cards match. */
const todos = computed<TodoItem[]>(() => {
  const raw = props.args["todos"];
  if (!Array.isArray(raw)) return [];
  const out: TodoItem[] = [];
  for (const item of raw) {
    if (item === null || typeof item !== "object") continue;
    const obj = item as Record<string, unknown>;
    const content = obj["content"];
    const status = obj["status"];
    if (typeof content !== "string" || content.trim() === "") continue;
    if (typeof status !== "string" || !VALID_STATUS.has(status)) continue;
    const priority = obj["priority"];
    out.push({
      content: content.trim(),
      status: status as TodoItem["status"],
      ...(priority === "high" || priority === "medium" || priority === "low"
        ? { priority }
        : {}),
    });
  }
  return out;
});

const doneCount = computed(
  () => todos.value.filter((x) => x.status === "completed").length,
);

const expanded = ref(true);

const STATUS_ICON: Record<TodoItem["status"], string> = {
  pending: "○",
  in_progress: "◐",
  completed: "●",
  cancelled: "✕",
};

function statusLabel(status: TodoItem["status"]): string {
  return t(`chat.todoStatus.${status}`);
}
</script>

<template>
  <div
    v-if="todos.length > 0"
    class="task-card"
    data-testid="task-list-card"
  >
    <button
      type="button"
      class="task-card-header"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    >
      <span class="task-card-title">
        <svg
          class="task-card-glyph"
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path d="M9 11l3 3L22 4" />
          <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
        </svg>
        {{ t("chat.taskListTitle") }}
        <span class="task-card-count">{{ doneCount }}/{{ todos.length }}</span>
      </span>
      <span class="task-card-chevron">{{ expanded ? "▴" : "▾" }}</span>
    </button>
    <ul
      v-if="expanded"
      class="task-card-list"
    >
      <li
        v-for="(item, idx) in todos"
        :key="idx"
        class="task-card-item"
        :class="`task-status-${item.status}`"
      >
        <span
          class="task-card-item-icon"
          :title="statusLabel(item.status)"
          :aria-label="statusLabel(item.status)"
        >{{ STATUS_ICON[item.status] }}</span>
        <span class="task-card-item-text">{{ item.content }}</span>
      </li>
    </ul>
  </div>
</template>
