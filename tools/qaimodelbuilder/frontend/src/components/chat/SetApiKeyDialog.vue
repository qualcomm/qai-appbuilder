<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * SetApiKeyDialog — in-place "set cloud provider API key" modal.
 *
 * Internal-edition enhancement: cloud models ship pre-configured but without
 * an API key. This dialog lets the user set the key directly from the chat
 * welcome screen (via `CloudModelApiKeyOnboarding`) instead of navigating to
 * Settings → Cloud Models. Saving has the same effect as the Settings entry
 * (PUT the provider config with the real `api_key`).
 *
 * Modeled 1:1 on `RenameDialog.vue` — the ONLY sanctioned text-input dialog
 * pattern (AGENTS.md §3.9): `<Teleport to="body">`, the global
 * `.rename-dialog-overlay` / `.rename-dialog*` classes, `useFocusTrap`,
 * Enter/Esc handling with an IME guard, a backdrop-dismiss guard, and a
 * confirm button disabled while loading or empty. The one addition over
 * RenameDialog is a masked password input with an eye toggle
 * (👁 / 🙈) mirroring `CloudModelsPanel.vue`'s provider-key field.
 */
import { ref, watch, computed, toRef } from "vue";
import { useI18n } from "vue-i18n";
import { useFocusTrap } from "@/composables/useFocusTrap";

interface Props {
  visible: boolean;
  /** provider_id whose key is being set (passed through to `save`'s caller). */
  providerId: string;
  /** Optional human-readable provider label shown in the subtitle. */
  providerLabel?: string;
  loading?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  providerLabel: "",
  loading: false,
});

const emit = defineEmits<{
  "update:visible": [value: boolean];
  "save": [key: string];
  "cancel": [];
}>();

const { t } = useI18n();

const inputRef = ref<HTMLInputElement | null>(null);
const dialogEl = ref<HTMLElement | null>(null);
const value = ref("");
const keyVisible = ref(false);

// Trap Tab focus inside the dialog + restore focus to the opener on close
// (same as RenameDialog).
useFocusTrap(dialogEl, { active: toRef(props, "visible"), focusFirst: true });

// Reset the input + visibility each time the dialog opens, and autofocus the
// input (RenameDialog parity: explicit focus on the semantic primary target).
watch(
  () => props.visible,
  (v) => {
    if (v) {
      value.value = "";
      keyVisible.value = false;
      setTimeout(() => inputRef.value?.focus(), 0);
    }
  },
);

const canSave = computed(() => !props.loading && value.value.trim().length > 0);

function toggleKeyVisible(): void {
  keyVisible.value = !keyVisible.value;
}

function doCancel(): void {
  emit("cancel");
  emit("update:visible", false);
}

function doSave(): void {
  if (!canSave.value) return;
  emit("save", value.value.trim());
}

// Enter submits, but never while the IME is composing a CJK candidate
// (RenameDialog IME-guard parity) and never while empty/loading.
function onEnter(event: KeyboardEvent): void {
  if (event.isComposing) return;
  doSave();
}

// Backdrop-dismiss guard (RenameDialog parity): a dismiss must be a FULL
// click that both starts AND ends on the overlay itself. Also: never dismiss
// via backdrop while a save is in flight.
const pointerDownOnOverlay = ref(false);

function onOverlayPointerDown(event: PointerEvent): void {
  pointerDownOnOverlay.value = event.target === event.currentTarget;
}

function onOverlayClick(event: MouseEvent): void {
  if (
    !props.loading &&
    event.target === event.currentTarget &&
    pointerDownOnOverlay.value
  ) {
    doCancel();
  }
  pointerDownOnOverlay.value = false;
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="rename-dialog-overlay"
      @pointerdown="onOverlayPointerDown"
      @click="onOverlayClick"
    >
      <div
        ref="dialogEl"
        class="rename-dialog"
        role="dialog"
        aria-modal="true"
        :aria-label="t('cloudModels.apiKeyDialog.title')"
        data-testid="set-api-key-dialog"
      >
        <div class="rename-dialog-title">
          🔑 {{ t("cloudModels.apiKeyDialog.title") }}
        </div>
        <div class="rename-dialog-subtitle">
          {{ providerLabel || t("cloudModels.apiKeyDialog.subtitle") }}
        </div>
        <div class="set-api-key-input-row">
          <input
            ref="inputRef"
            class="rename-dialog-input set-api-key-input"
            :type="keyVisible ? 'text' : 'password'"
            :value="value"
            :placeholder="t('cloudModels.apiKeyDialog.placeholder')"
            autocomplete="off"
            :disabled="loading"
            @input="value = ($event.target as HTMLInputElement).value"
            @keydown.enter="onEnter($event)"
            @keydown.esc="doCancel"
          />
          <button
            type="button"
            class="config-eye-btn set-api-key-eye"
            :title="keyVisible ? t('cloudModels.hide') : t('cloudModels.show')"
            :disabled="loading"
            @click="toggleKeyVisible"
          >
            {{ keyVisible ? "🙈" : "👁" }}
          </button>
        </div>
        <div class="rename-dialog-footer">
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--cancel"
            :disabled="loading"
            @click="doCancel"
          >
            {{ t("cloudModels.apiKeyDialog.cancel") }}
          </button>
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--confirm"
            :disabled="!canSave"
            @click="doSave"
          >
            {{ t("cloudModels.apiKeyDialog.save") }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/*
 * Layout-only additions on top of the shared global `.rename-dialog*`
 * classes (styles/components/components.css): the masked input needs to sit
 * next to the eye-toggle button. The input itself keeps the global
 * `.rename-dialog-input` look; we only make it flex within the row.
 */
.set-api-key-input-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  /* Mirror the global .rename-dialog-input bottom margin on the row wrapper
     so spacing to the footer matches RenameDialog exactly. */
  margin-bottom: var(--space-3);
}

.set-api-key-input {
  flex: 1 1 auto;
  min-width: 0;
  /* The global .rename-dialog-input has a bottom margin used to space it from
     the footer; keep that spacing on the row wrapper instead. */
  margin-bottom: 0;
}

.set-api-key-eye {
  flex-shrink: 0;
}
</style>
