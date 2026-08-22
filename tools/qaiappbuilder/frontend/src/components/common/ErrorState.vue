<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Error state primitive.
 *
 * S5 PR-052: views render this when a fetch fails. It displays the
 * translated `error.title` heading, the supplied message, and an
 * optional Retry button that emits `retry`.
 */
import { useI18n } from "vue-i18n";

const props = withDefaults(
  defineProps<{
    title?: string;
    message?: string;
    /** When true (default), show a Retry button. */
    retryable?: boolean;
  }>(),
  { title: "", message: "", retryable: true },
);

const emit = defineEmits<{
  retry: [];
}>();

const { t } = useI18n();
</script>

<template>
  <div
    class="error-state"
    role="alert"
  >
    <h3 class="error-state__title">
      {{ props.title === "" ? t("error.title") : props.title }}
    </h3>
    <p
      v-if="props.message !== ''"
      class="error-state__message"
    >
      {{ props.message }}
    </p>
    <button
      v-if="props.retryable"
      type="button"
      class="error-state__retry"
      @click="emit('retry')"
    >
      {{ t("error.retry") }}
    </button>
  </div>
</template>

<style scoped>
.error-state {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-primary);
}

.error-state__title {
  margin: 0;
  font-size: var(--text-lg);
  color: var(--text-primary);
}

.error-state__message {
  margin: 0;
  color: var(--text-muted);
}

.error-state__retry {
  align-self: flex-start;
  padding: var(--space-1) var(--space-3);
  border: 1px solid var(--border);
  background: var(--bg-primary);
  color: var(--text-primary);
  border-radius: 6px;
  cursor: pointer;
}

.error-state__retry:hover {
  background: var(--accent);
  color: var(--user-text);
  border-color: var(--accent);
}
</style>
