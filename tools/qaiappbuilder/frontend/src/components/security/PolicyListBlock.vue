<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * PolicyListBlock — one V1 "Allow Lists" category block.
 *
 * Renders a single category (read_allow / write_allow / exec_allow_cwd /
 * exec_deny_patterns) as the V1 `sec-cfg-list-block`: a title + mono field
 * key, a description, a ＋Add button, one editable row per entry with a ✕
 * delete, and an empty-state line. Stateless — all mutations are emitted to
 * the parent (the usePolicyLists composable owns the draft).
 */
import type { ListField } from "@/composables/usePolicyLists";
import { useI18n } from "vue-i18n";

const { t } = useI18n();

defineProps<{
  field: ListField;
  /** Localised category title (e.g. "可读路径"). */
  title: string;
  /** Localised description paragraph. */
  desc: string;
  /** Localised input placeholder / hint. */
  hint: string;
  /** The entries for this category. */
  entries: string[];
  /** Localised "add" button label. */
  addLabel: string;
  /** Localised empty-state text. */
  emptyLabel: string;
  disabled?: boolean;
}>();

const emit = defineEmits<{
  (e: "add"): void;
  (e: "update", idx: number, value: string): void;
  (e: "remove", idx: number): void;
}>();
</script>

<template>
  <div class="sec-cfg-list-block">
    <div class="sec-cfg-list-header">
      <div>
        <div class="sec-cfg-list-title">
          {{ title }}
        </div>
        <div class="sec-cfg-list-key mono">
          {{ field }}
        </div>
      </div>
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        :disabled="disabled"
        @click="emit('add')"
      >
        ＋ {{ addLabel }}
      </button>
    </div>
    <div class="sec-cfg-list-desc">
      {{ desc }}
    </div>
    <div class="sec-cfg-list">
      <div
        v-for="(entry, idx) in entries"
        :key="field + '-' + idx"
        class="sec-cfg-list-row"
      >
        <input
          class="sec-cfg-list-input mono"
          :value="entry"
          :placeholder="hint"
          :disabled="disabled"
          @input="emit('update', idx, ($event.target as HTMLInputElement).value)"
        />
        <button
          type="button"
          class="btn btn-ghost btn-sm sec-cfg-list-del"
          :disabled="disabled"
          :title="t('common.remove')"
          @click="emit('remove', idx)"
        >
          ✕
        </button>
      </div>
      <div
        v-if="!entries.length"
        class="sec-cfg-list-empty"
      >
        {{ emptyLabel }}
      </div>
    </div>
  </div>
</template>
