<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ServicePathRow — one "label + path input + Browse…" row of the Service
 * page's local-path section.
 *
 * There is deliberately NO save button: picking a path in the native dialog
 * saves it right away, and typing saves on blur / Enter (`change`). Both
 * rows of `ServicePathsSection` render through this one component so they
 * can never drift apart in behaviour or look.
 *
 * Contract with the parent: `pick` (optional) opens the native picker and
 * resolves the chosen path (`null` = cancelled); `save` persists a path and resolves the
 * value to display afterwards, or `null` when it refused it (the parent has
 * already toasted why) — the row then falls back to the last good value.
 */
import { ref, watch } from "vue";
import { useI18n } from "vue-i18n";

const props = defineProps<{
  label: string;
  /** Last persisted value, shown in the input. */
  modelValue: string;
  placeholder: string;
  /**
   * Opens a native picker and resolves the chosen path (`null` = cancelled).
   * Optional: when absent no Browse button is rendered and the path is typed.
   */
  pick?: () => Promise<string | null>;
  save: (value: string) => Promise<string | null>;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", value: string): void;
}>();

const { t } = useI18n();

const draft = ref(props.modelValue);
const busy = ref(false);

watch(
  () => props.modelValue,
  (v) => {
    draft.value = v;
  },
);

async function commit(value: string): Promise<void> {
  const next = value.trim();
  if (busy.value || next === props.modelValue) {
    draft.value = props.modelValue;
    return;
  }
  busy.value = true;
  try {
    const saved = await props.save(next);
    const shown = saved ?? props.modelValue;
    draft.value = shown;
    emit("update:modelValue", shown);
  } finally {
    busy.value = false;
  }
}

async function onBrowse(): Promise<void> {
  if (busy.value || !props.pick) return;
  const picked = await props.pick();
  if (picked) await commit(picked);
}
</script>

<template>
  <div class="paths-row">
    <label class="paths-label">{{ label }}</label>
    <input
      v-model="draft"
      class="param-input paths-input"
      :placeholder="placeholder"
      :disabled="busy"
      @change="commit(draft)"
    />
    <button
      v-if="pick"
      type="button"
      class="btn btn-ghost btn-sm"
      :disabled="busy"
      @click="onBrowse"
    >
      <span
        v-if="busy"
        class="spinner svc-btn-spinner"
        aria-hidden="true"
      ></span>
      {{ t("service.browse") }}
    </button>
  </div>
</template>

<style scoped>
.paths-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.paths-label {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  min-width: 180px;
}
.paths-input {
  flex: 1;
  min-width: 240px;
}
.param-input {
  padding: var(--space-2) var(--space-3);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: var(--text-sm);
}
.svc-btn-spinner {
  width: 12px;
  height: 12px;
  margin-right: 4px;
}
</style>
