<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * KeyValueTextarea — KEY=VALUE-per-line editor bound to a dict.
 *
 * Binds `Record<string, string>` via v-model. Each line `KEY=VALUE`
 * becomes one entry; lines without `=` or with an empty key are
 * ignored. Used for CC `session_env` and OC `provider_mapping`.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";

const { t } = useI18n();

const props = defineProps<{
  modelValue: Record<string, string>;
  placeholder?: string;
  minHeight?: string;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", value: Record<string, string>): void;
}>();

function dictToText(dict: Record<string, string>): string {
  return Object.entries(dict ?? {})
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

function textToDict(raw: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || !trimmed.includes("=")) continue;
    const eqIdx = trimmed.indexOf("=");
    const key = trimmed.slice(0, eqIdx).trim();
    const val = trimmed.slice(eqIdx + 1).trim();
    if (key) result[key] = val;
  }
  return result;
}

const text = computed<string>({
  get() {
    return dictToText(props.modelValue ?? {});
  },
  set(raw: string) {
    emit("update:modelValue", textToDict(raw));
  },
});

const count = computed(() => Object.keys(props.modelValue ?? {}).length);
</script>

<template>
  <div class="qai-kv-textarea">
    <textarea
      v-model="text"
      class="config-input mono"
      :placeholder="placeholder"
      :style="{ minHeight: minHeight ?? '90px', resize: 'vertical' }"
    ></textarea>
    <div class="qai-kv-textarea__count">
      {{ t("aiCoding.config.entriesCount", { count }) }}
    </div>
  </div>
</template>

<style scoped>
.qai-kv-textarea {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.qai-kv-textarea textarea {
  width: 100%;
  font-size: var(--text-sm, 0.85rem);
}

.qai-kv-textarea__count {
  font-size: var(--text-xs, 0.75rem);
  color: var(--text-muted);
}
</style>
