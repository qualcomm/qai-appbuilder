<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ConversationWorkspaceDialog — session-level workspace (write directory)
 * setting modal.
 *
 * V2 enhancement (no V1 equivalent): lets a conversation override the global
 * default write directory with its own session-level workspace. Structurally
 * a sibling of `RenameDialog.vue` — single-line text input + cancel/confirm
 * footer + Enter/Escape shortcuts + focus trap + backdrop-dismiss guard — so
 * it inherits the same V1-parity look via the global `.rename-dialog*` classes
 * (`styles/components/components.css`). It adds a hint line under the input
 * ("leave empty to use the global default directory").
 *
 * Unlike rename, the confirm button is NOT disabled on an empty input: an
 * empty value is a meaningful action (clear the per-session override and fall
 * back to the global default).
 *
 * AGENTS.md §3.9.2: project-defined dialog — the native `window.prompt` is
 * forbidden project-wide.
 */
import { ref, watch, computed, toRef } from "vue";
import { useI18n } from "vue-i18n";
import { useFocusTrap } from "@/composables/useFocusTrap";

interface Props {
  visible: boolean;
  modelValue: string;
  loading?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  loading: false,
});

const emit = defineEmits<{
  "update:modelValue": [value: string];
  "confirm": [];
  "cancel": [];
}>();

const { t } = useI18n();
const inputRef = ref<HTMLInputElement | null>(null);
const dialogEl = ref<HTMLElement | null>(null);

// Trap Tab focus inside the dialog and restore focus on close (RenameDialog
// parity).
useFocusTrap(dialogEl, { active: toRef(props, "visible"), focusFirst: true });

const displayTitle = computed(() => t("sessionWorkspace.title"));

// Auto-focus the input when the dialog becomes visible.
watch(
  () => props.visible,
  (v) => {
    if (v) {
      setTimeout(() => inputRef.value?.focus(), 0);
    }
  },
);

function onInput(event: Event): void {
  emit("update:modelValue", (event.target as HTMLInputElement).value);
}

// Backdrop-dismiss guard (RenameDialog.vue parity): a dismiss must be a full
// click that BOTH starts AND ends on the overlay element, so a drag-select in
// the input or a button press never spuriously closes the dialog.
const pointerDownOnOverlay = ref(false);

function onOverlayPointerDown(event: PointerEvent): void {
  pointerDownOnOverlay.value = event.target === event.currentTarget;
}

function onOverlayClick(event: MouseEvent): void {
  if (event.target === event.currentTarget && pointerDownOnOverlay.value) {
    emit("cancel");
  }
  pointerDownOnOverlay.value = false;
}

// Enter confirms unless loading or composing a CJK IME candidate. Empty input
// is allowed (clears the override), so there is no `.trim()` guard here.
function onEnter(event: KeyboardEvent): void {
  if (event.isComposing) return;
  if (!props.loading) {
    emit("confirm");
  }
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
        :aria-label="displayTitle"
      >
        <div class="rename-dialog-title">
          📁 {{ displayTitle }}
        </div>
        <input
          ref="inputRef"
          type="text"
          class="rename-dialog-input"
          :value="modelValue"
          :placeholder="t('sessionWorkspace.placeholder')"
          @input="onInput"
          @keydown.enter="onEnter($event)"
          @keydown.esc="emit('cancel')"
        />
        <!-- Hint: leaving the input empty falls back to the global default. -->
        <div class="conv-workspace-hint">
          {{ t("sessionWorkspace.hint") }}
        </div>
        <div class="rename-dialog-footer">
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--cancel"
            @click="emit('cancel')"
          >
            {{ t("sessionWorkspace.cancel") }}
          </button>
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--confirm"
            :disabled="loading"
            @click="emit('confirm')"
          >
            {{ loading ? t("sessionWorkspace.saving") : t("sessionWorkspace.confirm") }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* Hint line under the path input. Mirrors the subtitle proportions of the
   global `.rename-dialog-subtitle` but tinted as a secondary hint and given a
   little top margin so it reads as guidance for the input above it. */
.conv-workspace-hint {
  margin: -4px 0 12px;
  font-size: var(--text-xs);
  line-height: 1.5;
  color: var(--text-secondary);
}
</style>
