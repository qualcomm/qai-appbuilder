<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * NotificationCenter — persistent unread notification center for
 * scheduled-task results (方案乙).
 *
 * Bell button + unread-count badge; clicking toggles a dropdown panel
 * that lists each unread notification (icon + task name + result
 * preview) with a per-item action and ✕ (dismiss), plus a "mark all
 * read" action. The action depends on the task's scope: a
 * CONVERSATION-bound task jumps to its target conversation (opening or
 * switching its tab) and dismisses the entry; a GLOBAL task has no
 * conversation, so it opens a modal with the full result text fetched
 * from the task's run history and is dismissed only once the user
 * closes that modal (so it cannot vanish mid-read).
 *
 * De-dup / per-item removal logic lives in the notifications store; the
 * list-row + per-row ✕ chrome mirrors CodingQueueList.vue.
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import {
  useNotificationsStore,
  type NotificationItem,
} from "@/stores/notifications";
import { useChatTabsStore } from "@/stores/chatTabs";
import {
  useScheduledTasksStore,
  type ScheduledTaskRun,
} from "@/stores/scheduledTasks";
import { renderMarkdown } from "@/composables/markdown";

const { t } = useI18n();
const notifications = useNotificationsStore();
const chatTabs = useChatTabsStore();
const scheduledTasks = useScheduledTasksStore();

const open = ref(false);
const unreadCount = computed(() => notifications.unreadCount);

function toggle(): void {
  open.value = !open.value;
}

function close(): void {
  open.value = false;
}

/** ⏰ default, ✅ ok, ❌ failed. */
function iconFor(ok: boolean): string {
  return ok ? "✅" : "❌";
}

/** The global notification whose full result modal is open (null = closed). */
const resultItem = ref<NotificationItem | null>(null);
/** Text shown in that modal (the preview first, then the fetched full run). */
const resultText = ref("");
/**
 * Rendered result HTML for the modal body. Global-task results are LLM
 * output in Markdown (headings, lists, links, code fences, tables); the
 * plain-``<pre>`` render used to show them as raw source with ``##`` markers
 * and unwrapped URLs. Route them through the SAME ``renderMarkdown``
 * pipeline the chat bubbles use (marked + DOMPurify + hljs), so the modal
 * matches the visual quality of the assistant's live bubble. ``breaks: true``
 * mirrors the assistant-bubble render option so single newlines survive as
 * ``<br>`` — matches how the LLM's whitespace intent renders in-chat.
 * Empty preview falls back to the localised empty-state label (plain text).
 */
const renderedResultHtml = computed<string>(() => {
  if (resultText.value === "") return "";
  return renderMarkdown(resultText.value, {
    markedOptions: { breaks: true },
  });
});

/** True for a global task: no conversation to jump to, so show the result. */
function isGlobalItem(item: NotificationItem): boolean {
  return item.isGlobal || item.conversationId === "";
}

/**
 * Per-item action. A GLOBAL task's result is not folded into any chat, so
 * open it inline: fetch the newest run and show its full `result_text`
 * (falling back to the list preview, then to the empty-result label). The
 * entry is marked read on modal CLOSE, not here, so it does not disappear
 * from the list while the user is still reading it. A CONVERSATION-bound
 * task jumps to its target conversation — reusing an already-open tab
 * (switchTab) or opening one — and is dismissed immediately.
 */
function openItem(item: NotificationItem): void {
  if (isGlobalItem(item)) {
    resultItem.value = item;
    resultText.value = item.resultPreview;
    void scheduledTasks
      .loadRuns(item.taskId, 1)
      .then((runs: ScheduledTaskRun[]) => {
        // Ignore a late response for a modal the user already closed/reopened.
        if (resultItem.value?.id !== item.id) return;
        const newest = runs[0];
        if (newest !== undefined && newest.result_text !== "") {
          resultText.value = newest.result_text;
        }
      })
      .catch(() => {
        // Run history unreachable — the preview we already set is the fallback.
      });
    return;
  }
  const tab = chatTabs.tabs.find((x) => x.conversationId === item.conversationId);
  if (tab !== undefined) {
    chatTabs.switchTab(tab.id);
  } else {
    chatTabs.openTab({ conversationId: item.conversationId });
  }
  void notifications.markRead(item.id);
}

/** Close the result modal and only THEN mark the entry read. */
function closeResult(): void {
  const item = resultItem.value;
  resultItem.value = null;
  resultText.value = "";
  if (item !== null) void notifications.markRead(item.id);
}

function dismiss(id: string): void {
  void notifications.markRead(id);
}

function markAll(): void {
  void notifications.markAllRead();
}
</script>

<template>
  <div class="notif" data-testid="notification-center">
    <button
      type="button"
      class="notif__bell"
      :title="t('chat.notificationsTitle')"
      :aria-label="t('chat.notificationsTitle')"
      data-testid="notification-bell"
      @click="toggle"
    >
      <span class="notif__bell-icon">🔔</span>
      <span
        v-if="unreadCount > 0"
        class="notif__badge"
        data-testid="notification-badge"
        >{{ unreadCount }}</span
      >
    </button>

    <div v-if="open" class="notif__panel" data-testid="notification-panel">
      <div class="notif__head">
        <span class="notif__title">{{ t("chat.notificationsTitle") }}</span>
        <button
          v-if="unreadCount > 0"
          type="button"
          class="notif__markall"
          data-testid="notification-markall"
          @click="markAll"
        >
          {{ t("chat.notificationMarkAll") }}
        </button>
        <button
          type="button"
          class="notif__close"
          :aria-label="t('common.close')"
          :title="t('common.close')"
          data-testid="notification-close"
          @click="close"
        >
          ✕
        </button>
      </div>

      <p
        v-if="unreadCount === 0"
        class="notif__empty"
        data-testid="notification-empty"
      >
        {{ t("chat.notificationsEmpty") }}
      </p>

      <div
        v-for="item in notifications.items"
        v-else
        :key="item.id"
        class="notif__item"
      >
        <span class="notif__item-icon">{{ iconFor(item.ok) }}</span>
        <div class="notif__item-body">
          <span class="notif__item-name">{{ item.taskName }}</span>
          <span v-if="item.resultPreview !== ''" class="notif__item-preview">{{
            item.resultPreview
          }}</span>
        </div>
        <button
          type="button"
          class="notif__view"
          data-testid="notification-view"
          @click="openItem(item)"
        >
          {{
            isGlobalItem(item)
              ? t("scheduledTasks.viewResult")
              : t("chat.notificationView")
          }}
        </button>
        <button
          type="button"
          class="notif__dismiss"
          :aria-label="t('chat.notificationDismiss')"
          :title="t('chat.notificationDismiss')"
          data-testid="notification-dismiss"
          @click="dismiss(item.id)"
        >
          ✕
        </button>
      </div>
    </div>
    <!-- Full-result modal for a GLOBAL task (no conversation to jump to).
         Teleported to body so it overlays the page instead of being clipped
         by the dropdown panel's scroll container; mirrors the
         `sched-modal*` chrome used by ScheduledTasksPanel's edit modal. -->
    <Teleport to="body">
      <div
        v-if="resultItem !== null"
        class="notif-modal-overlay"
        data-testid="notification-result-modal"
        @click.self="closeResult"
      >
        <div class="notif-modal" role="dialog" aria-modal="true">
          <header class="notif-modal__head">
            <h4 class="notif-modal__title">
              {{
                t("scheduledTasks.resultTitle", { name: resultItem.taskName })
              }}
            </h4>
            <button
              type="button"
              class="notif__close"
              :aria-label="t('common.close')"
              :title="t('common.close')"
              @click="closeResult"
            >
              ✕
            </button>
          </header>

          <div class="notif-modal__body">
            <!-- Empty state stays plain text (no markdown to render). Real
                 result text is LLM output in Markdown; render it through the
                 same pipeline as the chat bubbles so the modal matches the
                 assistant-bubble look. eslint-disable v-html is safe here —
                 ``renderMarkdown`` runs the output through DOMPurify (see
                 ``@/composables/markdown``). -->
            <p v-if="resultText === ''" class="notif-modal__empty">
              {{ t("scheduledTasks.emptyResult") }}
            </p>
            <!-- eslint-disable vue/no-v-html -->
            <div
              v-else
              class="notif-modal__markdown"
              v-html="renderedResultHtml"
            />
            <!-- eslint-enable vue/no-v-html -->
          </div>

          <footer class="notif-modal__footer">
            <button
              type="button"
              class="notif__view"
              data-testid="notification-result-close"
              @click="closeResult"
            >
              {{ t("scheduledTasks.closeRuns") }}
            </button>
          </footer>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.notif {
  /* Inline in the topbar action row (flex sibling of the other action
   * buttons), NOT a fixed overlay — a fixed bell overlapped the tab-strip /
   * header buttons and would collide with any other top-right floating UI.
   * `relative` anchors the absolutely-positioned dropdown panel below it. */
  position: relative;
  display: inline-flex;
  align-items: center;
}
.notif__bell {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: 1px solid var(--border, #2a2750);
  background: var(--bg-secondary, #161430);
  border-radius: 50%;
  cursor: pointer;
  font-size: 16px;
}
.notif__bell:hover {
  background: var(--bg-hover, #201d3d);
}
.notif__bell-icon {
  line-height: 1;
}
.notif__badge {
  position: absolute;
  top: -4px;
  right: -4px;
  min-width: 16px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0 4px;
  font-size: 10px;
  font-weight: 700;
  color: #0d1b2a;
  background: var(--warning, #fbbf24);
  border-radius: 8px;
}
.notif__panel {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  z-index: 1000;
  width: 340px;
  max-height: 70vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  background: var(--bg-secondary, #161430);
  border: 1px solid var(--border, #2a2750);
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
}
.notif__head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.notif__title {
  flex: 1;
  font-size: var(--text-sm, 13px);
  font-weight: 600;
  color: var(--text-primary, #e9e7ff);
}
.notif__markall {
  flex-shrink: 0;
  border: none;
  background: transparent;
  color: var(--text-muted, #9b97c4);
  cursor: pointer;
  font-size: var(--text-xs, 11px);
}
.notif__markall:hover {
  color: var(--text-primary, #e9e7ff);
}
.notif__close {
  flex-shrink: 0;
  border: none;
  background: transparent;
  color: var(--text-muted, #9b97c4);
  cursor: pointer;
  font-size: var(--text-sm, 13px);
  line-height: 1;
  padding: 0 2px;
}
.notif__close:hover {
  color: var(--text-primary, #e9e7ff);
}
.notif__empty {
  margin: 0;
  padding: 12px 4px;
  text-align: center;
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #9b97c4);
}
.notif__item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px;
  background: var(--bg-primary, #0f0d24);
  border: 1px solid var(--border, #2a2750);
  border-radius: 8px;
}
.notif__item-icon {
  flex-shrink: 0;
  font-size: 14px;
  line-height: 1.4;
}
.notif__item-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.notif__item-name {
  font-size: var(--text-xs, 12px);
  font-weight: 600;
  color: var(--text-primary, #e9e7ff);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.notif__item-preview {
  font-size: 11px;
  color: var(--text-muted, #9b97c4);
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.notif__view {
  flex-shrink: 0;
  border: 1px solid var(--border, #2a2750);
  background: transparent;
  color: var(--accent, #8b7dff);
  cursor: pointer;
  font-size: var(--text-xs, 11px);
  border-radius: 6px;
  padding: 2px 8px;
}
.notif__view:hover {
  background: var(--bg-hover, #201d3d);
}
.notif__dismiss {
  flex-shrink: 0;
  border: none;
  background: transparent;
  color: var(--text-muted, #9b97c4);
  cursor: pointer;
  font-size: var(--text-xs, 11px);
}
.notif__dismiss:hover {
  color: #f87171;
}
/* Global-task full-result modal — teleported to body, so it mirrors the
 * `sched-modal*` overlay chrome from ScheduledTasksPanel rather than the
 * dropdown panel's absolute positioning. */
.notif-modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 9000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4, 16px);
  background: rgba(0, 0, 0, 0.5);
}
.notif-modal {
  width: 100%;
  /* Widen + heighten so a full LLM report reads comfortably. Cap by
     viewport (90/85 vw/vh) so it never overflows small screens, and by
     a max px so it does not stretch grotesquely wide on 4K monitors. */
  max-width: min(90vw, 960px);
  max-height: 85vh;
  min-height: 320px;
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary, #161430);
  border: 1px solid var(--border, #2a2750);
  border-radius: var(--radius-lg, 12px);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
}
.notif-modal__head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: var(--space-3, 12px) var(--space-4, 16px);
  border-bottom: 1px solid var(--border, #2a2750);
}
.notif-modal__title {
  flex: 1;
  margin: 0;
  font-size: var(--text-sm, 14px);
  font-weight: 600;
  color: var(--text-primary, #e9e7ff);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.notif-modal__body {
  padding: var(--space-4, 16px);
  overflow-y: auto;
  /* Body owns the scroll — head + footer stay pinned. */
  flex: 1 1 auto;
}
.notif-modal__empty {
  margin: 0;
  font-size: var(--text-sm, 13px);
  color: var(--text-secondary, #9d99c8);
  font-style: italic;
}
/* Markdown container mirrors the chat bubble's typographic scale so a
   scheduled-task result reads like an assistant turn. Uses ``:deep()``
   because the HTML is v-html'd from :func:`renderMarkdown` and would
   otherwise miss scoped-CSS attributes. */
.notif-modal__markdown {
  color: var(--text-primary, #e9e7ff);
  font-size: var(--text-sm, 14px);
  line-height: 1.7;
  word-wrap: break-word;
  overflow-wrap: anywhere;
}
.notif-modal__markdown :deep(h1),
.notif-modal__markdown :deep(h2),
.notif-modal__markdown :deep(h3),
.notif-modal__markdown :deep(h4) {
  margin: var(--space-3, 12px) 0 6px;
  font-weight: 600;
  line-height: 1.35;
}
.notif-modal__markdown :deep(h1) { font-size: 1.35em; }
.notif-modal__markdown :deep(h2) { font-size: 1.2em; }
.notif-modal__markdown :deep(h3) { font-size: 1.08em; }
.notif-modal__markdown :deep(h4) { font-size: 1em; }
.notif-modal__markdown :deep(p) { margin: 6px 0; }
.notif-modal__markdown :deep(ul),
.notif-modal__markdown :deep(ol) {
  padding-left: var(--space-5, 20px);
  margin: 6px 0;
}
.notif-modal__markdown :deep(li) { margin: 3px 0; }
.notif-modal__markdown :deep(a) {
  color: var(--accent, #7c6cff);
  text-decoration: underline;
  word-break: break-all;
}
.notif-modal__markdown :deep(a:hover) { opacity: 0.85; }
.notif-modal__markdown :deep(blockquote) {
  border-left: 3px solid var(--accent, #7c6cff);
  padding: 2px 0 2px var(--space-3, 12px);
  margin: var(--space-2, 8px) 0;
  color: var(--text-secondary, #9d99c8);
}
.notif-modal__markdown :deep(code):not(:deep(pre code)) {
  background: rgba(108, 99, 255, 0.15);
  color: #a78bfa;
  padding: 1px 5px;
  border-radius: 3px;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.9em;
}
.notif-modal__markdown :deep(pre) {
  margin: var(--space-3, 12px) 0;
  padding: var(--space-3, 12px) var(--space-4, 16px);
  background: var(--bg-tertiary, #0f0d24);
  border: 1px solid var(--border, #2a2750);
  border-radius: var(--radius-sm, 6px);
  overflow-x: auto;
}
.notif-modal__markdown :deep(pre code) {
  background: transparent;
  color: inherit;
  padding: 0;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.88em;
  line-height: 1.5;
}
.notif-modal__markdown :deep(hr) {
  border: 0;
  border-top: 1px solid var(--border, #2a2750);
  margin: var(--space-3, 12px) 0;
}
.notif-modal__markdown :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: var(--space-2, 8px) 0;
  font-size: 0.95em;
}
.notif-modal__markdown :deep(th),
.notif-modal__markdown :deep(td) {
  border: 1px solid var(--border, #2a2750);
  padding: 6px 10px;
  text-align: left;
}
.notif-modal__markdown :deep(th) {
  background: var(--bg-tertiary, #0f0d24);
  font-weight: 600;
}
.notif-modal__markdown :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 6px;
}
.notif-modal__footer {
  display: flex;
  justify-content: flex-end;
  padding: var(--space-3, 12px) var(--space-4, 16px);
  border-top: 1px solid var(--border, #2a2750);
}
</style>
