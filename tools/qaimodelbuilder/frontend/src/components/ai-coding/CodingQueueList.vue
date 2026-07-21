<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CodingQueueList — pending-message queue display (V1 ccQueue parity,
 * useClaudeCode.js:101 + AiCodingPanel queue rows).
 *
 * V1 parity (AiCodingPanel.js:967-991):
 *   - Default expanded panel: header `⏳ 待处理队列 ({n}/10)` + per-item
 *     row (序号 + 文本 + ✕)
 *   - Collapsed badge: `⏳ 待处理 N` — clicking re-expands the panel
 * Toggling preserves queue contents; only chrome visibility flips.
 */
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import type { QueuedMessage } from "@/composables/useCodingSession";

defineProps<{ items: QueuedMessage[] }>();
const emit = defineEmits<{ remove: [id: string] }>();

const { t } = useI18n();

/** Default expanded (V1 ccQueueExpanded ref, default true). */
const expanded = ref(true);
</script>

<template>
  <div
    v-if="items.length > 0"
    class="cc-queue"
    data-testid="cc-queue"
  >
    <!-- Expanded panel (V1 ccQueueExpanded === true) -->
    <template v-if="expanded">
      <button
        type="button"
        class="cc-queue__head"
        :title="t('aiCoding.panel.collapse', 'Collapse')"
        data-testid="cc-queue-collapse"
        @click="expanded = false"
      >
        <span>{{ t("aiCoding.panel.queueTitle", { n: items.length }) }}</span>
        <span class="cc-queue__chevron">▾</span>
      </button>
      <div
        v-for="(item, idx) in items"
        :key="item.id"
        class="cc-queue__item"
      >
        <span class="cc-queue__index">{{ idx + 1 }}</span>
        <span class="cc-queue__text">{{ item.text }}</span>
        <button
          type="button"
          class="cc-queue__remove"
          :aria-label="t('common.close', 'Remove')"
          @click="emit('remove', item.id)"
        >
          ✕
        </button>
      </div>
    </template>
    <!-- Collapsed badge (V1 .queue-badge) -->
    <button
      v-else
      type="button"
      class="cc-queue__badge"
      :title="t('aiCoding.panel.queueTitle', { n: items.length })"
      data-testid="cc-queue-expand"
      @click="expanded = true"
    >
      <span>{{ t("aiCoding.panel.queuePending") }}</span>
      <span class="cc-queue__badge-count">{{ items.length }}</span>
      <span class="cc-queue__chevron">▴</span>
    </button>
  </div>
</template>

<style scoped>
.cc-queue {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 10px;
  background: var(--bg-secondary, #161430);
  border: 1px dashed var(--border, #2a2750);
  border-radius: 8px;
}
.cc-queue__head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  background: transparent;
  border: none;
  padding: 0;
  font-size: var(--text-xs, 11px);
  font-weight: 600;
  color: var(--text-muted, #9b97c4);
  cursor: pointer;
  text-align: left;
}
.cc-queue__head > span:first-child {
  flex: 1;
}
.cc-queue__chevron {
  flex-shrink: 0;
  font-size: var(--text-xs, 11px);
  opacity: 0.8;
}
.cc-queue__item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-xs, 11px);
}
.cc-queue__index {
  flex-shrink: 0;
  min-width: 14px;
  height: 14px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 600;
  color: var(--text-muted, #9b97c4);
  background: var(--bg-hover);
  border-radius: 7px;
  padding: 0 4px;
}
.cc-queue__text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-primary, #e9e7ff);
}
.cc-queue__remove {
  flex-shrink: 0;
  border: none;
  background: transparent;
  color: var(--text-muted, #9b97c4);
  cursor: pointer;
  font-size: var(--text-xs, 11px);
}
.cc-queue__remove:hover {
  color: #f87171;
}
.cc-queue__badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  background: transparent;
  border: none;
  padding: 0;
  font-size: var(--text-xs, 11px);
  font-weight: 600;
  color: var(--warning, #fbbf24);
  cursor: pointer;
  text-align: left;
}
.cc-queue__badge-count {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 16px;
  height: 14px;
  font-size: 10px;
  color: #0d1b2a;
  background: var(--warning, #fbbf24);
  border-radius: 7px;
  padding: 0 4px;
}
</style>
