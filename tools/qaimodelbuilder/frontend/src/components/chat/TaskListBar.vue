<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * TaskListBar — compact top-right pill + dropdown for the LATEST task list
 * (V2 enhancement; V1 has no equivalent).
 *
 * Anchored to the chat conversation area's top-right corner via absolute
 * positioning (the positioning ancestor is `.chat-view`, same as the
 * stop-streaming float). It always reflects the active tab's most recent
 * `todowrite` (`tab.todoList`) so the user can check progress without
 * scrolling to the in-conversation snapshot card.
 *
 * Collapsed (default): a compact pill `<icon> done/total` that does NOT take
 * a full row — it floats over the top-right of the conversation.
 * Expanded: a dropdown panel drops DOWN from the pill, absolutely positioned
 * and elevated (shadow + high z-index) so it OVERLAYS the conversation
 * instead of pushing it down. Dismissed by clicking outside or pressing Esc.
 *
 * Open/closed state is per-tab (`tab.todoExpanded`, default false) so each
 * tab remembers its own state.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useClickOutside, useEscClose } from "@/composables/useClickOutside";
import type { TodoItem } from "@/stores/_chatTabsTypes";

const { t } = useI18n();
const store = useChatTabsStore();

const activeTab = computed(() => store.activeTab);
const todos = computed<TodoItem[]>(() => activeTab.value?.todoList ?? []);
const expanded = computed(() => activeTab.value?.todoExpanded ?? false);

const doneCount = computed(
  () => todos.value.filter((x) => x.status === "completed").length,
);
const allDone = computed(
  () => todos.value.length > 0 && doneCount.value === todos.value.length,
);

const STATUS_ICON: Record<TodoItem["status"], string> = {
  pending: "○",
  in_progress: "◐",
  completed: "●",
  cancelled: "✕",
};

function statusLabel(status: TodoItem["status"]): string {
  return t(`chat.todoStatus.${status}`);
}

function setExpanded(next: boolean): void {
  const id = activeTab.value?.id;
  if (id !== undefined) store.setTodoExpanded(id, next);
}
function toggle(): void {
  setExpanded(!expanded.value);
}
function close(): void {
  if (expanded.value) setExpanded(false);
}

// Dismiss the dropdown on outside click / Esc (only while open).
const rootEl = ref<HTMLElement | null>(null);
useClickOutside(rootEl, () => close(), { when: () => expanded.value });
useEscClose(() => close(), () => expanded.value);

// ── Publish the task-bar stack height ───────────────────────────────────
// The pending-message queue (and any future top-right floats) stack BELOW
// this bar. Because the bar is anchored top-right of `.chat-view` and the
// expandable dropdown is `position: absolute` (so it does NOT grow the bar's
// own box), we measure the visible extent — the lowest bottom edge of the
// pill OR the open dropdown — relative to `.chat-view`'s top, and publish it
// as `--qai-task-stack-bottom`. Downstream floats use it to sit just below
// the bar and to slide further down when the dropdown opens (per user spec).
// Same publish pattern as ChatComposer's `--qai-chat-input-h`.
function _findChatView(el: HTMLElement | null): HTMLElement | null {
  let cur: HTMLElement | null = el;
  while (cur !== null) {
    if (cur.classList.contains("chat-view")) return cur;
    cur = cur.parentElement;
  }
  return null;
}
let _resizeObs: ResizeObserver | null = null;
function _publishStackBottom(): void {
  const root = rootEl.value;
  const host = _findChatView(root);
  if (host === null) return;
  if (root === null || todos.value.length === 0) {
    // No bar rendered → nothing stacks below the bar; reset to 0 so floats
    // pin to the top of `.chat-view`.
    host.style.setProperty("--qai-task-stack-bottom", "0px");
    host.style.setProperty("--qai-task-pill-bottom", "0px");
    return;
  }
  const hostTop = host.getBoundingClientRect().top;
  // ── Full stack bottom (pill + dropdown when expanded) ────────────────────
  // Consumed by MessageQueuePanel so its floating queue sits fully below the
  // task-list dropdown. See MessageQueuePanel:16-19 for the contract.
  let maxBottom = root.getBoundingClientRect().bottom;
  const dropdown = root.querySelector<HTMLElement>(".task-bar-dropdown");
  if (dropdown !== null) {
    maxBottom = Math.max(maxBottom, dropdown.getBoundingClientRect().bottom);
  }
  const offset = Math.ceil(maxBottom - hostTop);
  host.style.setProperty("--qai-task-stack-bottom", `${offset}px`);
  // ── Pill-only bottom (excludes the dropdown) ─────────────────────────────
  // Consumed by the mode-intro-overlay anchor so its ⓘ button sits directly
  // BELOW the pill (avoiding overlap — user feedback: "the two buttons
  // overlap") without jumping down when the dropdown expands. The dropdown
  // occluding the intro button briefly is acceptable (user is looking at
  // tasks, not onboarding at that moment). See
  // styles/chat/chat.css `.chat-view__intro-anchor`.
  const pillBottom = Math.ceil(root.getBoundingClientRect().bottom - hostTop);
  host.style.setProperty("--qai-task-pill-bottom", `${pillBottom}px`);
}
onMounted(() => {
  if (typeof ResizeObserver !== "undefined") {
    _resizeObs = new ResizeObserver(() => _publishStackBottom());
    if (rootEl.value !== null) _resizeObs.observe(rootEl.value);
  }
  void nextTick(_publishStackBottom);
});
// Re-measure when the dropdown opens/closes or the task list changes (the
// dropdown is absolute, so ResizeObserver on the bar alone misses it).
watch([expanded, todos], () => {
  void nextTick(_publishStackBottom);
});
onBeforeUnmount(() => {
  if (_resizeObs !== null) {
    _resizeObs.disconnect();
    _resizeObs = null;
  }
  // Reset so a stale offset doesn't strand floats after this bar unmounts.
  const host = _findChatView(rootEl.value);
  if (host !== null) {
    host.style.removeProperty("--qai-task-stack-bottom");
    host.style.removeProperty("--qai-task-pill-bottom");
  }
});
</script>

<template>
  <div
    v-if="todos.length > 0"
    ref="rootEl"
    class="task-bar"
    data-testid="task-list-bar"
  >
    <button
      type="button"
      class="task-bar-pill"
      :class="{ 'task-bar-pill--done': allDone, 'task-bar-pill--open': expanded }"
      :aria-expanded="expanded"
      :title="t('chat.taskListTitle')"
      data-testid="task-list-pill"
      @click="toggle"
    >
      <svg
        class="task-bar-glyph"
        width="14"
        height="14"
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
      <span class="task-bar-count">{{ doneCount }}/{{ todos.length }}</span>
      <span class="task-bar-chevron">{{ expanded ? "▴" : "▾" }}</span>
    </button>

    <!-- Dropdown overlay: absolutely positioned below the pill, elevated so
         it covers the conversation rather than pushing it down. -->
    <div
      v-if="expanded"
      class="task-bar-dropdown"
      data-testid="task-list-dropdown"
    >
      <div class="task-bar-dropdown-head">
        <span
          class="task-bar-dropdown-title"
          :class="{ 'task-bar-dropdown-title--done': allDone }"
        >
          {{ t("chat.taskListProgress", { done: doneCount, total: todos.length }) }}
        </span>
      </div>
      <ul class="task-bar-list">
        <li
          v-for="(item, idx) in todos"
          :key="idx"
          class="task-bar-item"
          :class="`task-status-${item.status}`"
        >
          <span
            class="task-bar-item-icon"
            :title="statusLabel(item.status)"
            :aria-label="statusLabel(item.status)"
          >{{ STATUS_ICON[item.status] }}</span>
          <span class="task-bar-item-text">{{ item.content }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>
