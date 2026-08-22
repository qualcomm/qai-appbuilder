<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useActiveChatRuns } from "@/composables/chat/useActiveChatRuns";

const open = defineModel<boolean>("open", { default: false });
const { t } = useI18n();
const { runs, loading, error, refresh, openRun, stopRun } = useActiveChatRuns(open);
const rootEl = ref<HTMLElement | null>(null);

const countLabel = computed(() => {
  const count = runs.value.length;
  return count > 9 ? "9+" : String(count);
});

function onDocMouseDown(event: MouseEvent): void {
  if (!open.value) return;
  const root = rootEl.value;
  if (root !== null && !root.contains(event.target as Node)) {
    open.value = false;
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
  <span ref="rootEl" class="rit-active-runs-wrap">
    <button
      type="button"
      class="rit-btn rit-active-runs-btn"
      :class="{ 'rit-active-runs-btn--active': open || runs.length > 0 }"
      data-testid="active-runs-toggle"
      :title="t('chat.activeRuns.buttonTitle')"
      :aria-label="t('chat.activeRuns.buttonTitle')"
      :aria-pressed="open"
      @click="open = !open"
    >
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M3 12h4l2-7 4 14 2-7h6" />
      </svg>
      <span v-if="runs.length > 0" class="rit-active-runs-badge" aria-hidden="true">{{ countLabel }}</span>
    </button>

    <div
      v-if="open"
      class="rit-active-runs-menu"
      role="dialog"
      aria-modal="false"
      :aria-label="t('chat.activeRuns.title')"
      @mousedown.stop
      @keydown.esc="open = false"
    >
      <div class="rit-active-runs-head">
        <strong>{{ t("chat.activeRuns.title") }}</strong>
        <button type="button" class="rit-active-runs-refresh" :title="t('chat.activeRuns.refresh')" :aria-label="t('chat.activeRuns.refresh')" @click="() => refresh()">↻</button>
      </div>
      <div v-if="loading && runs.length === 0" class="rit-active-runs-state">{{ t("chat.activeRuns.loading") }}</div>
      <div v-else-if="error !== null && runs.length === 0" class="rit-active-runs-state rit-active-runs-state--error">{{ error === "open_unavailable" ? t("chat.activeRuns.openUnavailable") : t("chat.activeRuns.loadFailed") }}</div>
      <div v-else-if="runs.length === 0" class="rit-active-runs-state">{{ t("chat.activeRuns.empty") }}</div>
      <div v-else class="rit-active-runs-list">
        <div
          v-for="run in runs"
          :key="run.kind + ':' + run.id"
          class="rit-active-runs-item"
          :class="{ 'rit-active-runs-item--current': run.isCurrent, 'rit-active-runs-item--disabled': !run.openable }"
        >
          <button
            type="button"
            class="rit-active-runs-main rit-active-runs-open"
            :disabled="!run.openable"
            :title="run.openable ? t('chat.activeRuns.open') : t('chat.activeRuns.openUnavailable')"
            @click="openRun(run).then(() => { if (run.openable) open = false })"
          >
            <span class="rit-active-runs-title">{{ run.displayTitle }}</span>
            <span class="rit-active-runs-meta">
              <span class="rit-active-runs-dot" aria-hidden="true" />
              {{ run.aborted ? t("chat.activeRuns.stopping") : t("chat.activeRuns.running") }}
              <span>·</span>
              <span>{{ run.kind === "subagent" ? t("chat.activeRuns.subagentKind") : t("chat.activeRuns.chatKind") }}</span>
              <span v-if="run.isCurrent">· {{ t("chat.activeRuns.current") }}</span>
              <span v-else-if="run.isOpened">· {{ t("chat.activeRuns.opened") }}</span>
            </span>
            <span v-if="run.model_id" class="rit-active-runs-model">{{ run.model_id }}</span>
          </button>
          <button type="button" class="rit-active-runs-stop" :title="t('chat.activeRuns.stop')" :aria-label="t('chat.activeRuns.stop')" @click.stop="stopRun(run)">■</button>
        </div>
      </div>
    </div>
  </span>
</template>
