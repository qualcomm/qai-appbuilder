<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * RenameDialog — V1 parity rename modal.
 *
 * 1:1 port of V1 `js/components/RenameDialog.js` (lines 1-86):
 *   - icon + title + optional subtitle header
 *   - single-line input with Enter/Escape keyboard shortcuts
 *   - cancel + confirm footer buttons (flex 1:1)
 *   - loading state shows i18n "saving..." on the confirm button
 *   - confirm disabled while loading or while input.trim() is empty
 *   - a full click on the overlay backdrop (pointerdown + click both on the
 *     overlay itself) emits `cancel` (see `onOverlayClick` for the guard that
 *     stops a drag/re-render from spuriously dismissing the dialog)
 *
 * Visual styling reuses the V1 global `.rename-dialog*` classes from
 * `styles/components/components.css` (lines 292-382, mirrored from
 * V1 `css/components.css` 287-376) so V2 matches V1 byte-for-byte
 * (320px width, 9000 z-index, var(--accent) confirm button, etc.).
 *
 * AGENTS.md §3.9: this is the ONLY sanctioned text-input dialog; the
 * native `window.prompt` is forbidden project-wide.
 */
import { ref, watch, computed, toRef } from "vue";
import { useI18n } from "vue-i18n";
import { useFocusTrap } from "@/composables/useFocusTrap";

interface Props {
  visible: boolean;
  modelValue: string;
  /** Custom title; falls back to i18n `renameDialog.title`. */
  title?: string;
  /** Emoji shown before the title — V1 default `✏️` (RenameDialog.js:33). */
  icon?: string;
  /** Optional secondary line under the title. */
  subtitle?: string;
  /** Custom placeholder; falls back to i18n `renameDialog.placeholder`. */
  placeholder?: string;
  loading?: boolean;
  /** Inline style passed through to the input (V1 prop `inputStyle`). */
  inputStyle?: string;
}

const props = withDefaults(defineProps<Props>(), {
  title: "",
  icon: "✏️",
  subtitle: "",
  placeholder: "",
  loading: false,
  inputStyle: "",
});

const emit = defineEmits<{
  "update:modelValue": [value: string];
  "confirm": [];
  "cancel": [];
}>();

const { t } = useI18n();
const inputRef = ref<HTMLInputElement | null>(null);
const dialogEl = ref<HTMLElement | null>(null);

// V1 parity (utils/focus-trap.js): trap Tab focus inside the dialog and
// restore focus to the opener on close. The composable owns Tab cycling +
// previously-focused-element restore; the input's @keydown.esc continues to
// emit `cancel` (kept on the input itself so the input handles Esc even if
// focus hasn't moved yet).
useFocusTrap(dialogEl, { active: toRef(props, "visible"), focusFirst: true });

// Display fallbacks — V1 RenameDialog.js:40-43 computed properties.
const displayTitle = computed(() => props.title || t("renameDialog.title"));
const displayPlaceholder = computed(
  () => props.placeholder || t("renameDialog.placeholder"),
);

// Auto-focus input when the dialog becomes visible (V1 lines 45-53).
// useFocusTrap moves initial focus to the first focusable descendant
// (focusFirst: true) which is the input itself, but keep this explicit focus
// for V1 parity (the input is the semantic primary target).
watch(
  () => props.visible,
  (v) => {
    if (v) {
      // nextTick equivalent — wait until the input is mounted.
      setTimeout(() => inputRef.value?.focus(), 0);
    }
  },
);

function onInput(event: Event): void {
  emit("update:modelValue", (event.target as HTMLInputElement).value);
}

// Backdrop-dismiss guard (bug1 fix).
//
// V1 closed on `@click.self` (RenameDialog.js:56) — a single `click` whose
// target is the overlay itself. V2 added a per-conversation "live status dot"
// to the sidebar (AppSidebar.vue:821-828) which makes the conversation list
// re-render + re-sort on every streaming frame. Mid-rename, a `.conv-item`
// can move out from under the pointer between mousedown and mouseup, so a
// `click` whose *down* started on the dialog/input/button can `mouseup` on the
// teleported full-screen overlay — and `@click.self` then fired a spurious
// `cancel`, closing the dialog before the user confirmed.
//
// Fix: a backdrop dismiss must be a *full* click that BOTH starts AND ends on
// the overlay element. We record the pointerdown target and only emit `cancel`
// when the subsequent click's target is the overlay AND the press also began
// on the overlay. A press that began inside the dialog (drag-select in the
// input, button press) never dismisses, regardless of where the pointer is
// released. Accessibility is preserved: a genuine click on the empty backdrop
// (down + up both on the overlay) still closes the dialog, matching V1.
const pointerDownOnOverlay = ref(false);

function onOverlayPointerDown(event: PointerEvent): void {
  pointerDownOnOverlay.value = event.target === event.currentTarget;
}

function onOverlayClick(event: MouseEvent): void {
  // `click` target must be the overlay itself (V1 `@click.self` semantics)
  // AND the press must have started on the overlay (new guard).
  if (event.target === event.currentTarget && pointerDownOnOverlay.value) {
    emit("cancel");
  }
  pointerDownOnOverlay.value = false;
}

// V1 line 67: Enter only fires confirm when not loading AND input has content.
//
// IME guard (V2 hardening parallel to ChatComposer.vue:563 +
// V1 app.js:3317): when the user is composing a CJK candidate via the IME,
// the Enter that selects the candidate must NOT also confirm the rename
// (otherwise picking the first candidate prematurely commits the rename).
// V1 RenameDialog.js:67 missed this guard — V2 hardens it here.
function onEnter(event: KeyboardEvent): void {
  if (event.isComposing) return;
  if (!props.loading && props.modelValue.trim()) {
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
        <!-- V1 line 58: title row = icon + space + title (no separate header element) -->
        <div class="rename-dialog-title">
          {{ icon }} {{ displayTitle }}
        </div>
        <div
          v-if="subtitle"
          class="rename-dialog-subtitle"
        >
          {{ subtitle }}
        </div>
        <!-- V1 lines 60-69: single text input with v-model + Enter/Esc -->
        <input
          ref="inputRef"
          type="text"
          class="rename-dialog-input"
          :style="inputStyle"
          :value="modelValue"
          :placeholder="displayPlaceholder"
          @input="onInput"
          @keydown.enter="onEnter($event)"
          @keydown.esc="emit('cancel')"
        />
        <!-- V1 lines 70-77: footer with cancel + confirm buttons -->
        <div class="rename-dialog-footer">
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--cancel"
            @click="emit('cancel')"
          >
            {{ t("renameDialog.cancel") }}
          </button>
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--confirm"
            :disabled="loading || !modelValue.trim()"
            @click="emit('confirm')"
          >
            {{ loading ? t("renameDialog.saving") : t("renameDialog.confirm") }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
