<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * UserMessageJumpPopover — jump to any user message in the active conversation.
 *
 * Solves the "I sent something 30 turns ago, where is it?" problem in long
 * sessions. Anchored above the composer toolbar (immediately LEFT of the
 * `.rit-history` clock button); structurally mirrors `PromptHistoryPopover.vue`
 * (`open` prop + `update:open` emit, outside-mousedown closes, Escape closes).
 *
 * Each row shows `#N  HH:MM  <first 60 chars of content>` ellipsised when the
 * content overflows the row width. Click emits `jump` with the message id; the
 * host (ChatComposer → ChatView → ChatMessageList) handles the actual scroll.
 *
 * Filters out non-user-authored noise that would clutter the list:
 *   - role !== "user"                      → assistant / system / tool frames
 *   - msg.isCommandMsg === true            → slash-command echoes (not sent)
 *   - msg.meta.injected && msg.meta.pending → mid-turn injection bubbles
 *                                             that have not committed yet
 *
 * Styles intentionally not scoped: reuses the global `.rit-prompt-history-menu`
 * / `.riph-*` tokens from `frontend/src/styles/chat/chat.css` so the popover is
 * visually identical to its sibling history popover (light/dark safe; no
 * per-component magic colors).
 */
import { computed, onBeforeUnmount, onMounted, useTemplateRef } from "vue";
import { useI18n } from "vue-i18n";
import { useChatTabsStore } from "@/stores/chatTabs";
import type { ChatMessage } from "@/stores/_chatTabsTypes";

interface Props {
  open: boolean;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  "update:open": [value: boolean];
  /** User picked a message; payload is the stable `ChatMessage.id` UUID. */
  jump: [messageId: string];
}>();

const { t } = useI18n();
const store = useChatTabsStore();
const popoverRef = useTemplateRef<HTMLDivElement>("popover");

/** Row preview length — first 60 chars + ellipsis when truncated (2x the
 * original 30 to match the widened popover, showing more of each message). */
const PREVIEW_LEN = 60;

interface UserMessageRow {
  id: string;
  index: number; // 1-based display index in submission order
  time: string; // HH:MM
  preview: string;
  truncated: boolean;
  full: string; // tooltip on hover
}

function isPendingInjection(msg: ChatMessage): boolean {
  const meta = msg.meta as Record<string, unknown> | undefined;
  if (meta === undefined) return false;
  return meta["injected"] === true && meta["pending"] === true;
}

function formatTime(ts: number): string {
  // ChatMessage.createdAt is unix-ms (number). Build a HH:MM string in the
  // user's local zone — same format used by other chat surfaces.
  if (!Number.isFinite(ts) || ts <= 0) return "";
  const d = new Date(ts);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

function buildPreview(content: string): { preview: string; truncated: boolean } {
  // Strip markdown image syntax (`![alt](url)`) so an attached image renders as
  // its alt text (or nothing) instead of a long `/api/images/...` link — the
  // preview should show what the user typed, not the image link.
  const noImages = content.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
  // Collapse whitespace runs (newlines + multi-space) into a single space so
  // a multi-line prompt renders as a single readable row. Then slice to the
  // PREVIEW_LEN cap. `Array.from` so emoji / surrogate pairs are not split.
  const flat = noImages.replace(/\s+/g, " ").trim();
  // Image-only message (no typed text after stripping): show a placeholder
  // instead of a blank row.
  if (flat === "") {
    return { preview: t("chat.imageOnly", "[Image]"), truncated: false };
  }
  const chars = Array.from(flat);
  if (chars.length <= PREVIEW_LEN) {
    return { preview: flat, truncated: false };
  }
  return { preview: chars.slice(0, PREVIEW_LEN).join(""), truncated: true };
}

const rows = computed<UserMessageRow[]>(() => {
  const tab = store.activeTab;
  if (tab === null) return [];
  const out: UserMessageRow[] = [];
  let idx = 0;
  for (const msg of tab.messages) {
    if (msg.role !== "user") continue;
    if (msg.isCommandMsg === true) continue;
    if (isPendingInjection(msg)) continue;
    idx += 1;
    const { preview, truncated } = buildPreview(msg.content);
    out.push({
      id: msg.id,
      index: idx,
      time: formatTime(msg.createdAt),
      preview,
      truncated,
      full: msg.content,
    });
  }
  return out;
});

const isEmpty = computed(() => rows.value.length === 0);

function pick(id: string): void {
  emit("jump", id);
  close();
}

function close(): void {
  emit("update:open", false);
}

function onDocMouseDown(ev: MouseEvent): void {
  if (!props.open) return;
  const el = popoverRef.value;
  if (el === null) return;
  const target = ev.target as Node | null;
  if (target === null) return;
  if (el.contains(target)) return;
  // The toggle button is the popover's PARENT (the `.rit-user-jump-wrap`
  // span): clicks on the button must NOT also close us — let the button's
  // own click handler flip `open` instead, avoiding the double-toggle race.
  const wrap = el.parentElement;
  if (wrap !== null && wrap.contains(target)) return;
  close();
}

function onKeydown(ev: KeyboardEvent): void {
  if (ev.key === "Escape" && props.open) {
    ev.stopPropagation();
    close();
  }
}

onMounted(() => {
  document.addEventListener("mousedown", onDocMouseDown, true);
});
onBeforeUnmount(() => {
  document.removeEventListener("mousedown", onDocMouseDown, true);
});
</script>

<template>
  <div
    v-if="open"
    ref="popover"
    class="rit-prompt-history-menu rit-user-jump-menu"
    role="dialog"
    :aria-label="t('userMessageJump.title', '跳转到我的消息')"
    data-testid="user-message-jump-popover"
    @mousedown.stop
    @keydown="onKeydown"
  >
    <div class="riph-group">
      <div class="riph-group-head">
        <span class="riph-group-title">
          {{ t("userMessageJump.title", "跳转到我的消息") }}
        </span>
        <span
          v-if="!isEmpty"
          class="rumj-count"
        >{{ rows.length }}</span>
      </div>
      <div
        v-if="isEmpty"
        class="riph-empty"
        data-testid="user-message-jump-empty"
      >
        {{ t("userMessageJump.empty", "本会话还没有发过消息") }}
      </div>
      <ul
        v-else
        class="riph-list"
      >
        <li
          v-for="row in rows"
          :key="row.id"
          class="riph-row"
          data-testid="user-message-jump-row"
        >
          <button
            type="button"
            class="riph-row-text rumj-row-btn"
            :title="row.full"
            @click="pick(row.id)"
          >
            <span
              class="rumj-index"
              aria-hidden="true"
            >#{{ row.index }}</span>
            <span
              v-if="row.time !== ''"
              class="rumj-time"
              aria-hidden="true"
            >{{ row.time }}</span>
            <span class="rumj-preview">{{ row.preview }}{{ row.truncated ? "…" : "" }}</span>
          </button>
        </li>
      </ul>
    </div>
  </div>
</template>

<!-- Styles intentionally not scoped: see component docstring. The popover
     reuses `.rit-prompt-history-menu` / `.riph-*` tokens; only the row-inner
     layout (index pill + time + preview) is additive and lives next to its
     siblings in chat.css. -->
