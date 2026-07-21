<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Loading state primitive.
 *
 * S5 PR-052: minimal spinner + label, used by views during their
 * initial data fetch. The label defaults to `common.loading` from
 * vue-i18n; callers can override with the `label` prop.
 */
import { useI18n } from "vue-i18n";

const props = withDefaults(
  defineProps<{
    label?: string;
    /** Render in compact (inline) mode for use inside other widgets. */
    compact?: boolean;
  }>(),
  { label: "", compact: false },
);

const { t } = useI18n();
</script>

<template>
  <div
    class="loading-state"
    :class="{ 'loading-state--compact': props.compact }"
    role="status"
    aria-live="polite"
  >
    <span
      class="loading-state__spinner"
      aria-hidden="true"
    />
    <span class="loading-state__label">
      {{ props.label === "" ? t("common.loading") : props.label }}
    </span>
  </div>
</template>

<style scoped>
.loading-state {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  padding: var(--space-5);
  color: var(--text-muted);
}

.loading-state--compact {
  padding: var(--space-2);
}

.loading-state__spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: loading-state-spin 0.8s linear infinite;
}

@keyframes loading-state-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
