<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * WhisperEnginePopover — voice ASR engine picker.
 *
 * Mirrors V1's `rit-voice-engine-menu` block (frontend/index.html
 * lines 1099-1183). The popover lists the engine catalog
 * (`VOICE_ENGINES`) from `useVoiceInput`. Clicking an item:
 *   1. updates `engineId` (persisted to localStorage)
 *   2. calls `setEngine` which writes the choice through to
 *      `PUT /api/app-builder/voice-preference`
 *   3. closes the popover
 *
 * T2.7-D — adds a per-item Ready / Loading / Cold status badge driven
 * by `loadedModels` / `preloadState` from the shared composable, plus
 * a cold-start hint at the bottom (V1 line 1180).
 */
import { onMounted, onBeforeUnmount, useTemplateRef, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useVoiceInput, VOICE_ENGINES } from "@/composables/useVoiceInput";

interface Props {
  open: boolean;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  "update:open": [value: boolean];
}>();

const { t } = useI18n();
const {
  engineId,
  setEngine,
  loadedModels,
  preloadState,
  refreshWorkerStatus,
} = useVoiceInput();

const popoverRef = useTemplateRef<HTMLDivElement>("popover");

function pick(id: string): void {
  void setEngine(id);
  emit("update:open", false);
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
  const wrap = el.parentElement;
  if (wrap !== null && wrap.contains(target)) return;
  close();
}

// Refresh the worker/status snapshot every time the popover opens so the
// dots reflect current sticky-worker state without forcing the user to
// click the mic button first. Mirrors V1 line 1113's
// `voiceEngineMenuOpen && voiceInput.refreshWorkerStatus()`.
watch(
  () => props.open,
  (next: boolean) => {
    if (next) {
      void refreshWorkerStatus();
    }
  },
);

onMounted(() => {
  document.addEventListener("mousedown", onDocMouseDown, true);
});

onBeforeUnmount(() => {
  document.removeEventListener("mousedown", onDocMouseDown, true);
});

// V1 parity (index.html lines 1164-1177): per-engine status class.
//   'is-warm'    → modelId is in loadedModels                   (green dot)
//   'is-loading' → currently selected engine + preloadState='loading' (pulse)
//   '' (cold)    → not in loadedModels and not currently loading  (muted dot)
function statusClass(modelId: string, engId: string): string {
  if (loadedModels.value.some((m) => m.modelId === modelId)) {
    return "is-warm";
  }
  if (engineId.value === engId && preloadState.value === "loading") {
    return "is-loading";
  }
  return "";
}

function statusText(modelId: string, engId: string): string {
  if (loadedModels.value.some((m) => m.modelId === modelId)) {
    return t("voiceInput.warmBadge");
  }
  if (engineId.value === engId && preloadState.value === "loading") {
    return t("voiceInput.loadingBadge");
  }
  return t("voiceInput.coldBadge");
}
</script>

<template>
  <div
    v-if="open"
    ref="popover"
    class="rit-voice-engine-menu"
    role="menu"
    :aria-label="t('voiceInput.engineMenuTitle')"
    data-testid="whisper-engine-popover"
    @mousedown.stop
  >
    <div class="rit-voice-engine-menu-header">
      {{ t("voiceInput.engineMenuTitle") }}
    </div>
    <button
      v-for="e in VOICE_ENGINES"
      :key="e.id"
      type="button"
      role="menuitemradio"
      class="rit-voice-engine-menu-item"
      :class="{ 'is-selected': engineId === e.id }"
      :aria-checked="engineId === e.id ? 'true' : 'false'"
      :data-testid="`whisper-engine-item-${e.id}`"
      @click="pick(e.id)"
    >
      <span
        class="rit-voice-engine-menu-radio"
        aria-hidden="true"
      >
        <span
          v-if="engineId === e.id"
          class="rit-voice-engine-menu-radio-inner"
        />
      </span>
      <span class="rit-voice-engine-menu-label">
        {{ t(e.labelKey, e.label) }}
      </span>
      <span
        class="rit-voice-engine-menu-status"
        :class="statusClass(e.modelId, e.id)"
        :data-testid="`whisper-engine-status-${e.id}`"
      >
        {{ statusText(e.modelId, e.id) }}
      </span>
    </button>
    <div class="rit-voice-engine-menu-foot">
      {{ t("voiceInput.engineMenuHint") }}
    </div>
  </div>
</template>

<!-- Styles intentionally not scoped: the `.rit-voice-engine-menu*`
     selectors live in `frontend/src/styles/chat/chat.css` (migrated
     verbatim from V1 chat.css lines 756-873). Reusing those global
     class names keeps the popover visually identical to V1 and
     avoids the per-page tokens problem flagged in
     docs/90-refactor/archive/next-session-prompt-ui-per-page.md (focus rules / colors
     come from design tokens, not magic numbers). -->
