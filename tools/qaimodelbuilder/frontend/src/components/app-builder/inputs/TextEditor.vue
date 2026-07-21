<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * TextEditor — text input with character count and constraints.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";

const { t } = useI18n();

interface TextConstraints {
  maxLength?: number;
  minLength?: number;
  placeholder?: string;
  rows?: number;
}

interface Props {
  modelValue: string;
  constraints?: TextConstraints;
}

const props = withDefaults(defineProps<Props>(), {
  constraints: () => ({}),
});

const emit = defineEmits<{
  "update:modelValue": [value: string];
}>();

const charCount = computed(() => props.modelValue.length);
const maxLen = computed(() => props.constraints.maxLength ?? Infinity);
const isOverLimit = computed(() => charCount.value > maxLen.value);

function onInput(event: Event): void {
  const value = (event.target as HTMLTextAreaElement).value;
  emit("update:modelValue", value);
}
</script>

<template>
  <div class="text-editor">
    <textarea
      class="text-editor__textarea"
      :value="modelValue"
      :placeholder="constraints.placeholder ?? t('appBuilder.textPlaceholder')"
      :rows="constraints.rows ?? 6"
      @input="onInput"
    />
    <div class="text-editor__footer">
      <span
        class="text-editor__count"
        :class="{ 'text-editor__count--over': isOverLimit }"
      >
        {{ charCount }}{{ maxLen !== Infinity ? `/${maxLen}` : "" }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.text-editor {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.text-editor__textarea {
  width: 100%;
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-tertiary);
  color: var(--text-primary);
  font: inherit;
  font-size: var(--text-base);
  resize: vertical;
}

.text-editor__textarea:focus {
  outline: 2px solid var(--accent);
  outline-offset: -1px;
}

.text-editor__footer {
  display: flex;
  justify-content: flex-end;
}

.text-editor__count {
  font-size: 11px;
  color: var(--text-muted);
}

.text-editor__count--over {
  color: red;
}
</style>
