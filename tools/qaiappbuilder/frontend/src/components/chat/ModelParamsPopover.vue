<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModelParamsPopover — sampling-parameter dialog (T2.6-A).
 *
 * Mirrors V1's `model-params-popover` block (frontend/index.html
 * lines 1040-1097). The popover shows a "use defaults" toggle plus
 * temperature / top-p / max-tokens controls. Values are persisted in
 * the active tab's `modelParams` via the store; on send,
 * `useChatTransport` forwards them as additive query params on the
 * SSE URL (refactor-plan §3.1).
 */
import { computed, onMounted, onBeforeUnmount, useTemplateRef } from "vue";
import { useI18n } from "vue-i18n";
import {
  useChatTabsStore,
  DEFAULT_MODEL_PARAMS,
} from "@/stores/chatTabs";

interface Props {
  open: boolean;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  "update:open": [value: boolean];
}>();

const { t } = useI18n();
const store = useChatTabsStore();

const activeTab = computed(() => store.activeTab);

const params = computed(() => activeTab.value?.modelParams ?? DEFAULT_MODEL_PARAMS);

const popoverRef = useTemplateRef<HTMLDivElement>("popover");

function patch(patchObj: Partial<typeof DEFAULT_MODEL_PARAMS>): void {
  const tab = activeTab.value;
  if (tab === null) return;
  store.setModelParams(tab.id, patchObj);
}

function close(): void {
  emit("update:open", false);
}

// Click-outside handler — closes the popover when the user clicks
// outside of it. We use the capture-phase mousedown so we beat the
// trigger button's own click handler (which would re-toggle on top of
// our close), and we walk up the DOM looking for the trigger's wrapper
// (`.model-params-wrap`) so clicking the trigger does NOT immediately
// close + re-open in the same tick.
function onDocMouseDown(ev: MouseEvent): void {
  if (!props.open) return;
  const el = popoverRef.value;
  if (el === null) return;
  const target = ev.target as Node | null;
  if (target === null) return;
  if (el.contains(target)) return;
  // Walk up — if the click landed inside the same wrap as the popover,
  // it's the trigger button; let its click handler toggle.
  const wrap = el.parentElement;
  if (wrap !== null && wrap.contains(target)) return;
  close();
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
    class="model-params-popover"
    role="dialog"
    aria-modal="false"
    :aria-label="t('index.modelParamsTitleHeader')"
    data-testid="model-params-popover"
    @mousedown.stop
  >
    <!-- V1 parity (index.html:1056-1059): header is "🔧 {title}" + ✕ close. -->
    <div class="model-params-header">
      <span>🔧 {{ t("index.modelParamsTitleHeader") }}</span>
      <button
        type="button"
        class="model-params-close"
        :aria-label="t('common.close')"
        :title="t('common.close')"
        @click="close"
      >
        ✕
      </button>
    </div>

    <div class="model-params-body">
      <!-- V1 parity (index.html:1062-1065): "use model defaults" checkbox.
           When checked, the three rows below are dimmed + non-interactive
           via inline opacity/pointer-events (NOT a disabled attribute), so
           they match V1 exactly. -->
      <label
        class="model-params-row"
        style="cursor: pointer; gap: 8px"
      >
        <input
          type="checkbox"
          :checked="params.useDefaults"
          style="accent-color: var(--accent)"
          data-testid="model-params-use-defaults"
          @change="(ev: Event) => patch({ useDefaults: (ev.target as HTMLInputElement).checked })"
        />
        <span style="font-size: var(--text-sm)">
          {{ t("index.useModelDefaults") }}
        </span>
      </label>

      <!-- Temperature (V1 index.html:1066-1073) -->
      <div
        class="model-params-row"
        :style="params.useDefaults ? 'opacity:0.4;pointer-events:none' : ''"
      >
        <label class="model-params-label">{{ t("index.modelParamsTemperature") }}</label>
        <div class="model-params-slider-wrap">
          <input
            type="range"
            min="0"
            max="2"
            step="0.1"
            :value="params.temperature"
            class="model-params-slider"
            data-testid="model-params-temperature"
            @input="(ev: Event) => patch({ temperature: Number((ev.target as HTMLInputElement).value) })"
          />
          <span class="model-params-value">{{ params.temperature.toFixed(1) }}</span>
        </div>
      </div>

      <!-- Top P (V1 index.html:1074-1081) -->
      <div
        class="model-params-row"
        :style="params.useDefaults ? 'opacity:0.4;pointer-events:none' : ''"
      >
        <label class="model-params-label">{{ t("index.modelParamsTopP") }}</label>
        <div class="model-params-slider-wrap">
          <input
            type="range"
            min="0"
            max="1"
            step="0.05"
            :value="params.topP"
            class="model-params-slider"
            data-testid="model-params-top-p"
            @input="(ev: Event) => patch({ topP: Number((ev.target as HTMLInputElement).value) })"
          />
          <span class="model-params-value">{{ params.topP.toFixed(2) }}</span>
        </div>
      </div>

      <!-- Max Tokens (V1 index.html:1082-1091) -->
      <div
        class="model-params-row"
        :style="params.useDefaults ? 'opacity:0.4;pointer-events:none' : ''"
      >
        <label class="model-params-label">{{ t("index.modelParamsMaxTokens") }}</label>
        <div class="model-params-slider-wrap">
          <input
            type="number"
            min="0"
            max="1000000"
            step="256"
            :value="params.maxTokens"
            class="model-params-input"
            data-testid="model-params-max-tokens"
            :placeholder="t('index.zeroNoLimit')"
            @input="(ev: Event) => patch({ maxTokens: Math.max(0, Number((ev.target as HTMLInputElement).value)) })"
          />
        </div>
      </div>

      <!-- V1 parity (index.html:1092-1094): hint row, inline styling.
           V1 has NO reset button here — the "use defaults" checkbox is the
           reset affordance, so the V2-only reset button was removed. -->
      <div
        style="font-size: var(--text-xs); color: var(--text-muted); margin-top: 4px"
      >
        💡 {{ t("index.modelParamsHint") }}
      </div>
    </div>
  </div>
</template>

<!-- Styles intentionally not scoped: the `.model-params-*` selectors
     live in `frontend/src/styles/components/components.css` (migrated
     verbatim from V1 css/components.css lines 1123-1218). Reusing those
     global class names keeps the popover byte-for-byte identical to V1
     (min-width 280px / --bg-secondary / --border-light / --radius-md /
     --shadow-lg / toast-in animation) and avoids the per-component
     drift the previous scoped block introduced (380px width, custom
     shadow, opacity 0.45, extra reset button). -->

