<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * MessageQueuePanel — floating "pending sends" queue (V1 parity for
 * behaviour, index.html:864-890; V2 layout places it top-right).
 *
 * V1 lets the user keep pressing Enter while a turn is streaming; each
 * press enqueues the input (capped at MAX_QUEUE_SIZE). This panel
 * surfaces that queue as a floating overlay. Two forms:
 *   - Expanded: a 280px panel listing each pending message with a ✕
 *     delete button (V1 `.queue-panel`).
 *   - Collapsed: a pill badge showing the pending count (V1
 *     `.queue-badge`); clicking it re-expands.
 *
 * V2 layout (per user spec, 2026-06-17): unlike V1's bottom-right anchor,
 * the queue stacks at the TOP-RIGHT of `.chat-view`, directly BELOW the
 * task-list bar (`.task-bar`). Top-right floats stack top→down; when one
 * disappears the ones below slide up. Positioning lives in
 * `chat.css .queue-float`, which offsets its `top` by the
 * `--qai-task-stack-bottom` CSS var published by TaskListBar so it follows
 * the bar (and slides down when the bar's dropdown opens).
 *
 * State lives entirely in the chatTabs store (per-tab `messageQueue` /
 * `queueExpanded`) so this component stays presentational — it reads the
 * active tab's queue and dispatches store actions. The auto-dequeue /
 * re-send loop is owned by ChatView's streaming→idle watcher, NOT here.
 *
 * Renders nothing when the active tab has no queued messages (the wrapper
 * is `v-if`-guarded by the parent), matching V1's
 * `v-if="messageQueue.length > 0"`.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { useChatTabsStore, MAX_QUEUE_SIZE } from "@/stores/chatTabs";
import { useToast } from "@/composables/useToast";

const { t } = useI18n();
const store = useChatTabsStore();
const toast = useToast();

/**
 * `edit` — the user clicked a pending item's ✎ "edit" affordance: the item
 * has already been recalled (removed) from the queue here; the parent
 * (ChatView) routes `text` into the composer draft for re-editing. Kept as an
 * event (not a direct store/composer call) so this panel stays presentational
 * and does not reach across to the composer component.
 */
const emit = defineEmits<{ (e: "edit", text: string): void }>();

const activeTab = computed(() => store.activeTab);
const queue = computed(() => activeTab.value?.messageQueue ?? []);
const expanded = computed(() => activeTab.value?.queueExpanded ?? false);

function collapse(): void {
  const id = activeTab.value?.id;
  if (id !== undefined) store.setQueueExpanded(id, false);
}
function expand(): void {
  const id = activeTab.value?.id;
  if (id !== undefined) store.setQueueExpanded(id, true);
}
/** Cancel (delete) a pending item (the ✕ affordance). The queue holds only
 *  Enter-while-streaming messages (mid-turn injections are control-plane-only
 *  and never enter the queue), so this is a plain queue removal. */
function remove(queueId: string): void {
  const id = activeTab.value?.id;
  if (id === undefined) return;
  store.removeFromQueue(id, queueId);
}

/** Copy a pending item's text to the clipboard (the ⧉ affordance). The bubble
 *  is preserved (copy does not cancel). Uses the async Clipboard API with a
 *  toast on success/failure — never a silent no-op. */
async function copyItem(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(t("chat.queueCopied", "Copied"));
  } catch {
    toast.warning(t("chat.queueCopyFailed", "Copy failed"));
  }
}

/** Recall a pending item back to the composer for re-editing (the ✎
 *  affordance): remove it from the queue and hand its text to the parent via
 *  the `edit` event. This is the "cancel + re-edit" path (图一 edit icon). */
function editItem(queueId: string): void {
  const id = activeTab.value?.id;
  if (id === undefined) return;
  const text = store.recallFromQueue(id, queueId);
  if (text !== null) emit("edit", text);
}

/** Display label for a queued item. A normal item shows its text; an
 *  image-only item (blank text but an attached image, `imagePrefix` set)
 *  shows a "🖼 image" placeholder so the row is not visibly empty. */
function itemLabel(item: { text: string; imagePrefix: string }): string {
  if (item.text !== "") return item.text;
  if (item.imagePrefix !== "") return t("chat.queueImageOnly", "🖼 image");
  return item.text;
}
</script>

<template>
  <div
    v-if="queue.length > 0"
    class="queue-float"
    data-testid="message-queue-float"
  >
    <!-- Expanded panel -->
    <div
      v-if="expanded"
      class="queue-panel"
      data-testid="message-queue-panel"
    >
      <div
        class="queue-panel-header"
        :title="t('index.collapseShort')"
        style="cursor:pointer"
        @click="collapse"
      >
        <span>⏳ {{ t("index.queuePendingTitle") }} ({{ queue.length }}/{{ MAX_QUEUE_SIZE }})</span>
        <button
          type="button"
          class="queue-panel-close"
          :title="t('index.collapseShort')"
          @click.stop="collapse"
        >
          ▾
        </button>
      </div>
      <div class="queue-panel-list">
        <div
          v-for="(item, idx) in queue"
          :key="item.id"
          class="queue-panel-item"
        >
          <div class="queue-panel-item-index">
            {{ idx + 1 }}
          </div>
          <div
            class="queue-panel-item-text"
            :title="itemLabel(item)"
          >
            {{ itemLabel(item) }}
          </div>
          <button
            type="button"
            class="queue-panel-item-act"
            :title="t('index.copyShort', 'Copy')"
            :aria-label="t('index.copyShort', 'Copy')"
            @click="() => { void copyItem(item.text); }"
          >
            ⧉
          </button>
          <button
            type="button"
            class="queue-panel-item-act"
            :title="t('index.editShort', 'Edit')"
            :aria-label="t('index.editShort', 'Edit')"
            @click="editItem(item.id)"
          >
            ✎
          </button>
          <button
            type="button"
            class="queue-panel-item-del"
            :title="t('index.deleteShort')"
            :aria-label="t('index.deleteShort')"
            @click="remove(item.id)"
          >
            ✕
          </button>
        </div>
      </div>
    </div>
    <!-- Collapsed badge -->
    <div
      v-else
      class="queue-badge"
      data-testid="message-queue-badge"
      @click="expand"
    >
      <span>⏳ {{ t("index.queuePendingShort") }}</span>
      <span class="queue-badge-count">{{ queue.length }}</span>
      <span style="font-size:var(--text-xs);opacity:0.8">▴</span>
    </div>
  </div>
</template>
