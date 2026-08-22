<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Empty state primitive.
 *
 * S5 PR-052: shown by views when a list / panel has no content yet.
 * Body text falls back to `common.empty`.
 */
import { useI18n } from "vue-i18n";

const props = withDefaults(
  defineProps<{
    title?: string;
    body?: string;
  }>(),
  { title: "", body: "" },
);

const { t } = useI18n();
</script>

<template>
  <div
    class="empty-state"
    role="status"
  >
    <h3
      v-if="props.title !== ''"
      class="empty-state__title"
    >
      {{ props.title }}
    </h3>
    <p class="empty-state__body">
      {{ props.body === "" ? t("common.empty") : props.body }}
    </p>
    <slot />
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-5);
  color: var(--text-muted);
  text-align: center;
}

.empty-state__title {
  margin: 0;
  font-size: var(--text-lg);
  color: var(--text-primary);
}

.empty-state__body {
  margin: 0;
}
</style>
