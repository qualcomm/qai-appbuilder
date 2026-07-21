<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Toast host.
 *
 * S5 PR-052: renders the toast queue from `useToastStore`.
 *
 * Auto-dismiss (2026-06-11 fix): timers are owned HERE, the single render
 * point for every toast, so a toast auto-dismisses regardless of which entry
 * path pushed it. Previously only `useToast()` scheduled the timer; the many
 * call sites that push directly via `useToastStore().push()` (downloads /
 * service / install-delete toasts) had no timer and lingered until manually
 * closed. Centralising the timer on the host fixes them all without touching
 * ~30 call sites and keeps the store serialisable (no closures in state).
 * `store.dismiss` is idempotent, so a redundant `useToast()` timer is
 * harmless.
 *
 * V1 parity (alignment 2026-06-05): reuses the global toast CSS from
 * `styles/components/components.css` (`.toast-container` top-center
 * container, `.toast` + `.toast.success/.error/.info/.warning` colours,
 * `toast-in` entry animation, `.toast-close` button, `.toast-progress`
 * countdown bar). No component-scoped styling — the global classes are
 * the single source of truth, matching V1 `index.html:152-174`.
 *
 * 2026-07-17: container position moved from top-right → top-center so
 * toasts no longer occlude the top-bar action cluster (Workspace / Tool
 * Calls / Collapse / Export / Clear). See
 * `docs/30-ui-ux/ux-feedback-2026-07-implementation.md` §3.1.
 */
import { storeToRefs } from "pinia";
import { onBeforeUnmount, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useToastStore } from "@/stores/toast";

const { t } = useI18n();
const store = useToastStore();
const { items } = storeToRefs(store);

function dismiss(id: string): void {
  store.dismiss(id);
}

// Per-toast auto-dismiss timers, keyed by id. Owned by the host so every
// toast (whatever pushed it) gets a timer exactly once.
const timers = new Map<string, ReturnType<typeof setTimeout>>();

function clearTimer(id: string): void {
  const t = timers.get(id);
  if (t !== undefined) {
    clearTimeout(t);
    timers.delete(id);
  }
}

watch(
  items,
  (list) => {
    const liveIds = new Set(list.map((toast) => toast.id));
    // Drop timers for toasts that are already gone (manual close / clear).
    for (const id of [...timers.keys()]) {
      if (!liveIds.has(id)) clearTimer(id);
    }
    // Schedule a one-shot dismiss for any newly-seen, non-sticky toast.
    for (const toast of list) {
      if (toast.timeoutMs > 0 && !timers.has(toast.id)) {
        const handle = setTimeout(() => {
          timers.delete(toast.id);
          store.dismiss(toast.id);
        }, toast.timeoutMs);
        timers.set(toast.id, handle);
      }
    }
  },
  { deep: true, immediate: true },
);

onBeforeUnmount(() => {
  for (const handle of timers.values()) clearTimeout(handle);
  timers.clear();
});
</script>

<template>
  <div
    class="toast-container"
    role="region"
    aria-live="polite"
    :aria-label="t('toast.ariaLabel')"
  >
    <div
      v-for="toast in items"
      :key="toast.id"
      class="toast"
      :class="toast.kind"
      :role="toast.kind === 'error' || toast.kind === 'warning' ? 'alert' : 'status'"
    >
      <span
        v-if="toast.icon"
        aria-hidden="true"
      >{{ toast.icon }}</span>
      <span class="toast-message">{{ toast.message }}</span>
      <button
        type="button"
        class="toast-close"
        :aria-label="t('toast.closeTitle') + '：' + toast.message"
        :title="t('toast.closeTitle')"
        @click="dismiss(toast.id)"
      >
        ✕
      </button>
      <div
        v-if="toast.timeoutMs > 0"
        class="toast-progress"
        :style="{ animationDuration: toast.timeoutMs + 'ms' }"
        aria-hidden="true"
      ></div>
    </div>
  </div>
</template>
