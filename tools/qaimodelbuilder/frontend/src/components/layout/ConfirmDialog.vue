<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ConfirmDialog — global confirmation dialog host.
 *
 * V1 parity (`index.html` `.confirm-overlay`/`.confirm-dialog`). Mounted
 * once in `App.vue`; driven by the `confirm` store and resolved through
 * `useConfirm()`. Clicking the backdrop or Cancel resolves `false`;
 * Confirm resolves `true`. Escape also cancels.
 */
import { watch, onBeforeUnmount, ref } from "vue";
import { storeToRefs } from "pinia";
import { useConfirmStore } from "@/stores/confirm";
import { useConfirm } from "@/composables/useConfirm";
import { useFocusTrap } from "@/composables/useFocusTrap";

const store = useConfirmStore();
const { visible, icon, title, message, confirmText, cancelText, confirmStyle } =
  storeToRefs(store);
const { accept, cancel } = useConfirm();

// V1 parity (index.html .confirm-dialog used a focus-trap): keep Tab focus
// cycling inside the dialog while it is open, and restore focus to the
// previously-focused element on close. V2 previously only handled Esc/Enter.
//
// The trap implementation lives in `composables/useFocusTrap.ts` (extracted
// to share the V1 `utils/focus-trap.js` behaviour across every modal). This
// component keeps Esc/Enter as semantic confirm/cancel shortcuts (which the
// generic trap composable doesn't know about) and delegates Tab cycling +
// previously-focused-element restore to the composable.
const dialogEl = ref<HTMLElement | null>(null);
useFocusTrap(dialogEl, { active: visible, onEscape: () => cancel() });

function onKeydown(e: KeyboardEvent): void {
  if (!visible.value) return;
  if (e.key === "Enter") {
    e.preventDefault();
    accept();
  }
  // Esc is handled by useFocusTrap.onEscape; Tab cycling is handled by the
  // composable's keydown listener.
}

watch(visible, (v) => {
  if (v) {
    window.addEventListener("keydown", onKeydown);
  } else {
    window.removeEventListener("keydown", onKeydown);
  }
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeydown);
});
</script>

<template>
  <div
    v-if="visible"
    class="confirm-overlay"
    @click.self="cancel"
  >
    <div
      ref="dialogEl"
      class="confirm-dialog"
    >
      <div class="confirm-dialog-header">
        <span v-if="icon">{{ icon }}</span>
        <span>{{ title }}</span>
      </div>
      <div class="confirm-dialog-body">
        {{ message }}
      </div>
      <div class="confirm-dialog-footer">
        <button
          type="button"
          class="btn btn-ghost"
          @click="cancel"
        >
          {{ cancelText }}
        </button>
        <button
          type="button"
          :class="['btn', confirmStyle === 'danger' ? 'btn-danger' : 'btn-primary']"
          @click="accept"
        >
          {{ confirmText }}
        </button>
      </div>
    </div>
  </div>
</template>
