<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->
<script setup lang="ts">
import { ref, watch, toRef } from "vue";
import { useI18n } from "vue-i18n";

import { useFocusTrap } from "@/composables/useFocusTrap";
import { useVlaSimulation } from "@/composables/useVlaSimulation";

const props = defineProps<{ visible: boolean }>();
const emit = defineEmits<{ close: [] }>();

const { t } = useI18n();
const { serverUrl, saveServerUrl } = useVlaSimulation();
const dialogEl = ref<HTMLElement | null>(null);
const draftUrl = ref("");
const error = ref(false);
const pointerDownOnOverlay = ref(false);

useFocusTrap(dialogEl, { active: toRef(props, "visible"), focusFirst: true });

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      draftUrl.value = serverUrl.value;
      error.value = false;
    }
  },
);

function onSave(): void {
  if (!saveServerUrl(draftUrl.value)) {
    error.value = true;
    return;
  }
  emit("close");
}

function onOverlayPointerDown(event: PointerEvent): void {
  pointerDownOnOverlay.value = event.target === event.currentTarget;
}

function onOverlayClick(event: MouseEvent): void {
  if (event.target === event.currentTarget && pointerDownOnOverlay.value) {
    emit("close");
  }
  pointerDownOnOverlay.value = false;
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="vla-settings-overlay"
      data-testid="vla-settings-overlay"
      @pointerdown="onOverlayPointerDown"
      @click="onOverlayClick"
    >
      <form
        ref="dialogEl"
        class="vla-settings-dialog"
        role="dialog"
        aria-modal="true"
        :aria-label="t('index.vlaSimulationSettingsTitle')"
        @submit.prevent="onSave"
        @keydown.esc.stop.prevent="emit('close')"
      >
        <h2>{{ t("index.vlaSimulationSettingsTitle") }}</h2>
        <p class="vla-settings-intro">
          {{ t("index.vlaSimulationSettingsIntro") }}
        </p>
        <label class="vla-settings-label" for="vla-simulation-url">
          {{ t("index.vlaSimulationServerUrl") }}
        </label>
        <input
          id="vla-simulation-url"
          v-model="draftUrl"
          class="vla-settings-input"
          type="text"
          inputmode="url"
          autocomplete="url"
          :placeholder="t('index.vlaSimulationServerUrlPlaceholder')"
          data-testid="vla-simulation-url"
          :aria-invalid="error"
          @input="error = false"
        />
        <p class="vla-settings-hint">
          {{ t("index.vlaSimulationServerUrlHint") }}
        </p>
        <p v-if="error" class="vla-settings-error" role="alert">
          {{ t("index.vlaSimulationServerUrlInvalid") }}
        </p>
        <div class="vla-settings-footer">
          <button
            type="button"
            class="vla-settings-button vla-settings-button--cancel"
            data-testid="vla-settings-cancel"
            @click="emit('close')"
          >
            {{ t("renameDialog.cancel") }}
          </button>
          <button
            type="submit"
            class="vla-settings-button vla-settings-button--save"
            data-testid="vla-settings-save"
          >
            {{ t("index.vlaSimulationSave") }}
          </button>
        </div>
      </form>
    </div>
  </Teleport>
</template>

<style scoped>
.vla-settings-overlay {
  position: fixed;
  inset: 0;
  z-index: 9000;
  display: grid;
  place-items: center;
  padding: 16px;
  background: rgb(0 0 0 / 58%);
}

.vla-settings-dialog {
  width: min(560px, 100%);
  padding: 22px;
  color: var(--text-primary);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 18px 50px rgb(0 0 0 / 40%);
}

.vla-settings-dialog h2 {
  margin: 0 0 12px;
  font-size: 17px;
  font-weight: 650;
}

.vla-settings-intro,
.vla-settings-hint {
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.5;
}

.vla-settings-intro {
  margin: 0 0 18px;
}

.vla-settings-label {
  display: block;
  margin-bottom: 6px;
  font-size: 13px;
  font-weight: 600;
}

.vla-settings-input {
  box-sizing: border-box;
  width: 100%;
  padding: 9px 10px;
  color: var(--text-primary);
  font: inherit;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: 7px;
}

.vla-settings-input:focus {
  border-color: var(--accent);
  outline: 2px solid color-mix(in srgb, var(--accent) 28%, transparent);
}

.vla-settings-hint {
  margin: 7px 0 0;
}

.vla-settings-error {
  margin: 8px 0 0;
  color: var(--danger, #ef6464);
  font-size: 13px;
}

.vla-settings-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 22px;
}

.vla-settings-button {
  min-width: 84px;
  padding: 8px 14px;
  color: var(--text-primary);
  font: inherit;
  border: 1px solid var(--border);
  border-radius: 7px;
  cursor: pointer;
}

.vla-settings-button--cancel {
  background: transparent;
}

.vla-settings-button--save {
  color: var(--accent-contrast, #fff);
  background: var(--accent);
  border-color: var(--accent);
}
</style>
