<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * WorkingDirsTextarea — multi-line directory whitelist editor.
 *
 * Binds a `string[]` via v-model: each non-empty trimmed line maps to
 * one array entry. Used by both CC and OC config panels for
 * `allowed_working_dirs` / `add_dirs`.
 */
import { computed } from "vue";

const props = defineProps<{
  modelValue: string[];
  placeholder?: string;
  minHeight?: string;
  /** Show a red warning when the list is empty. */
  warnEmpty?: boolean;
  emptyWarning?: string;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", value: string[]): void;
}>();

const text = computed<string>({
  get() {
    return (props.modelValue ?? []).join("\n");
  },
  set(raw: string) {
    const dirs = raw
      .split("\n")
      .map((d) => d.trim())
      .filter((d) => d.length > 0);
    emit("update:modelValue", dirs);
  },
});

const isEmpty = computed(() => (props.modelValue ?? []).length === 0);
</script>

<template>
  <div class="qai-dirs-textarea">
    <textarea
      v-model="text"
      class="config-input mono"
      :placeholder="placeholder"
      :style="{ minHeight: minHeight ?? '80px', resize: 'vertical' }"
    ></textarea>
    <div
      v-if="warnEmpty && isEmpty && emptyWarning"
      class="qai-dirs-textarea__warn"
    >
      {{ emptyWarning }}
    </div>
  </div>
</template>

<style scoped>
.qai-dirs-textarea {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.qai-dirs-textarea textarea {
  width: 100%;
  font-size: var(--text-sm, 0.85rem);
}

.qai-dirs-textarea__warn {
  font-size: var(--text-sm, 0.85rem);
  color: var(--danger, #f87171);
}
</style>
