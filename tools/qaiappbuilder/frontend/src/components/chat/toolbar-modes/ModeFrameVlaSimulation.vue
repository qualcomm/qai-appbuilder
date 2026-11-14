<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->
<script setup lang="ts">
import { ref } from "vue";
import { useI18n } from "vue-i18n";

import { useVlaSimulation } from "@/composables/useVlaSimulation";
import VlaSimulationSettingsDialog from "@/components/chat/VlaSimulationSettingsDialog.vue";

const emit = defineEmits<{ exit: [] }>();
const { t } = useI18n();
const { serverUrl } = useVlaSimulation();
const settingsOpen = ref(false);
</script>

<template>
  <div class="rit-left" data-testid="mode-frame-vla-simulation">
    <button
      type="button"
      class="rit-mode-badge"
      data-testid="vla-mode-exit"
      :title="t('toolbar.exitMode', { mode: t('index.vlaSimulationMode') })"
      @click="emit('exit')"
    >
      <svg
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M4 5h16v14H4z" />
        <path d="M8 9h.01M16 9h.01M9 15h6" />
        <path d="M2 12h2m16 0h2" />
      </svg>
      <span>{{ t("index.vlaSimulationMode") }}</span>
      <span class="rit-close" aria-hidden="true">✕</span>
    </button>

    <span class="rit-sep"></span>

    <span
      class="vla-status"
      data-testid="vla-server-status"
      :title="serverUrl || t('index.vlaSimulationNotConfigured')"
    >
      <span
        class="vla-status-dot"
        :class="serverUrl ? 'vla-status-dot--configured' : 'vla-status-dot--empty'"
        aria-hidden="true"
      ></span>
      <span>{{ serverUrl || t("index.vlaSimulationNotConfigured") }}</span>
    </span>

    <button
      type="button"
      class="rit-btn"
      data-testid="vla-settings-btn"
      :title="t('index.vlaSimulationSettings')"
      @click="settingsOpen = true"
    >
      <svg
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      ><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
      <span>{{ t("index.vlaSimulationSettings") }}</span>
    </button>

    <a
      v-if="serverUrl"
      class="rit-btn"
      :href="serverUrl"
      target="_blank"
      rel="noopener noreferrer"
      data-testid="vla-open-server"
    >
      {{ t("index.vlaSimulationOpen") }}
    </a>

    <VlaSimulationSettingsDialog
      :visible="settingsOpen"
      @close="settingsOpen = false"
    />
  </div>
</template>

<style scoped>
.vla-status {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  max-width: min(42vw, 480px);
  overflow: hidden;
  color: var(--text-secondary);
  font-size: 12px;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.vla-status-dot {
  flex: 0 0 7px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.vla-status-dot--configured {
  background: #48b47a;
}

.vla-status-dot--empty {
  background: var(--text-muted, #7f8595);
}

.vla-status > span:last-child {
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
