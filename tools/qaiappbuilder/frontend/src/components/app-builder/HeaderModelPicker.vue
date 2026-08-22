<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * HeaderModelPicker — App Builder Setup Bar inline model picker (V1 parity).
 *
 * Button + popover. The button shows a status dot + selected model name + caret;
 * clicking toggles a popover that embeds ModelStrip (card grid). Selecting a
 * model emits `update:selectedId` and closes; the info action emits `info`
 * without closing.
 *
 * Uses global `.ab-model-picker*` / `.ab-status-dot*` classes from
 * `styles/app-builder/app-builder.css`.
 */
import { ref, computed } from "vue";
import { useI18n } from "vue-i18n";
import { useClickOutside, useEscClose } from "@/composables/useClickOutside";
import ModelStrip from "./ModelStrip.vue";
import type { AppModelCardVM } from "./types";

interface Props {
  /** Currently selected model id */
  selectedId?: string | null;
  /** All available models (lean /models rows) */
  models: Array<{ id: string; title: string; enabled: boolean; taxonomy: string[] }>;
}

const props = withDefaults(defineProps<Props>(), { selectedId: null });

const emit = defineEmits<{
  "update:selectedId": [id: string];
  info: [id: string];
}>();

const { t } = useI18n();

const open = ref(false);
const rootEl = ref<HTMLDivElement | null>(null);

/** Resolve selected model from list */
const selectedModel = computed<AppModelCardVM | null>(() => {
  const id = props.selectedId;
  if (!id) return null;
  // models prop uses `id`; ModelStrip/cards use `modelId`
  const found = props.models.find((m) => m.id === id);
  if (!found) return null;
  return {
    modelId: found.id,
    displayName: found.title || found.id,
    status: found.enabled ? "ready" : "notinstalled",
  };
});

/** Status class for the dot (status-ready / status-notinstalled / etc.) */
const statusClass = computed(() => {
  const m = selectedModel.value;
  if (!m) return "status-unknown";
  const s = String(m.status || "ready").toLowerCase();
  return `status-${s}`;
});

/** Label shown on the button */
const buttonLabel = computed(() => {
  const m = selectedModel.value;
  return m ? m.displayName : null;
});

function toggle() {
  open.value = !open.value;
}

function onSelect(id: string) {
  emit("update:selectedId", id);
  open.value = false;
}

function onInfo(model: AppModelCardVM) {
  emit("info", model.modelId);
  // info action does NOT close the picker — user may keep browsing
}

// Dismiss: outside-press (V1 parity uses mousedown capture, kept via the
// `event: "mousedown"` option) + ESC. Both gated by `open.value` so the
// listeners are no-ops while the picker is closed.
useClickOutside(
  rootEl,
  () => {
    open.value = false;
  },
  { event: "mousedown", when: () => open.value },
);
useEscClose(
  (ev) => {
    ev.preventDefault();
    open.value = false;
  },
  () => open.value,
);
</script>

<template>
  <div
    ref="rootEl"
    class="ab-model-picker"
    :class="{ open }"
  >
    <button
      type="button"
      class="ab-model-picker-button"
      :class="{ 'is-empty': !selectedModel, active: open }"
      :aria-expanded="open"
      aria-haspopup="listbox"
      :title="selectedModel ? selectedModel.modelId : t('appBuilder.choosingModel', 'Choose a model')"
      @click="toggle"
    >
      <span
        v-if="selectedModel"
        class="ab-status-dot"
        :class="statusClass"
        aria-hidden="true"
      />
      <span class="ab-model-picker-label">
        {{ buttonLabel || t("appBuilder.choosingModel", "Choose a model") }}
      </span>
      <span
        class="ab-model-picker-caret"
        aria-hidden="true"
      >&#x25BE;</span>
    </button>

    <div
      v-if="open"
      class="ab-model-picker-popover"
      role="dialog"
      :aria-label="t('appBuilder.aria.models')"
    >
      <ModelStrip
        :selected-id="selectedId"
        @select="onSelect"
        @info="onInfo"
      />
    </div>
  </div>
</template>
