<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ComposerModeButtons — default `.rit-left` toolbar (attach + tool modes)
 * (ARCH-1 cohesion split from `ChatComposer.vue`, zero behaviour change).
 *
 * V1 index.html:1481-1528. Renders the "+" attach button and the
 * data-driven tool-mode buttons (v-for over `visibleToolbarModules`, sorted
 * by `order`). Inline SVG icon selection mirrors V1 index.html:1492-1497.
 * This is the block shown only when no tool mode is active
 * (`effectiveMode === null`); the active-mode sub-toolbars stay in the
 * parent so the v-else-if chain is unchanged.
 *
 * Pure presentational + event-forwarding child: `attach` fires the
 * composer's `openFilePicker`; `pick-mode` fires `setToolModeFromKey`.
 */
import { useI18n } from "vue-i18n";
import type { ToolMode } from "@/stores/ui";
import type { VisibleToolbarModule } from "@/composables/useForgeConfig";
import ComposerModeIcon from "@/components/chat/composer/ComposerModeIcon.vue";

defineProps<{
  modules: VisibleToolbarModule[];
  effectiveMode: ToolMode | null;
}>();

const emit = defineEmits<{
  attach: [];
  "pick-mode": [mode: string];
}>();

const { t } = useI18n();
</script>

<template>
  <div class="rit-left">
    <button
      type="button"
      class="rit-btn rit-attach"
      :title="t('chat.attachImageTitle')"
      :aria-label="t('chat.attachImageTitle')"
      @click="emit('attach')"
    >
      <!-- V1 parity (index.html:1481): plus icon is an outline SVG
           (two 2.2px round-cap lines), NOT a text "+". -->
      <svg
        width="14"
        height="14"
        viewBox="0 0 14 14"
        fill="none"
        stroke="currentColor"
        stroke-width="2.2"
        stroke-linecap="round"
        aria-hidden="true"
      >
        <line
          x1="7"
          y1="1"
          x2="7"
          y2="13"
        />
        <line
          x1="1"
          y1="7"
          x2="13"
          y2="7"
        />
      </svg>
    </button>
    <span class="rit-sep"></span>
    <!-- Data-driven mode buttons: v-for over enabled modules,
         sorted by `order`. Disabling a module in Settings hides
         its button instantly (Pinia-shared state).
         Tooltip: when the module carries an optional `hint` i18n key
         (e.g. App Builder → `index.appBuilderHint`), show
         "Label — Hint" for discoverability; otherwise just the label. -->
    <button
      v-for="m in modules"
      :key="m.key"
      type="button"
      class="rit-btn"
      :class="['rit-tool-' + m.key, { 'rit-btn--active': effectiveMode === m.mode }]"
      :data-testid="`mode-btn-${m.mode}`"
      :title="m.hint ? t(m.i18n) + ' — ' + t(m.hint) : t(m.i18n)"
      :aria-label="m.hint ? t(m.i18n) + ' — ' + t(m.hint) : t(m.i18n)"
      @click="emit('pick-mode', m.mode)"
    >
      <!-- Shared icon component — single source of truth (also used by
           Settings → Toolbar Modules) so both lists stay in sync. -->
      <ComposerModeIcon :icon-key="m.key" />
      <span>{{ t(m.i18n) }}</span>
    </button>
  </div>
</template>

<style scoped>
/* Active tool button highlight (moved with the mode buttons from
 * ChatComposer's scoped block — not a global token). */
.rit-btn--active {
  color: var(--accent, #a594ff) !important;
  background: var(--accent-muted, rgba(124, 108, 240, 0.12));
}
</style>
