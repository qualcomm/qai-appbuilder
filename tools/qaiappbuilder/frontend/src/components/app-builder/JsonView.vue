<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * JsonView — recursive JSON tree viewer.
 */
import { ref, computed } from "vue";

interface Props {
  data: unknown;
  depth?: number;
  maxDepth?: number;
}

const props = withDefaults(defineProps<Props>(), {
  depth: 0,
  maxDepth: 6,
});

const collapsed = ref(props.depth > 1);

const isObject = computed(() => props.data !== null && typeof props.data === "object");
const isArray = computed(() => Array.isArray(props.data));

const entries = computed(() => {
  if (!isObject.value) return [];
  return Object.entries(props.data as Record<string, unknown>);
});

const displayValue = computed(() => {
  if (props.data === null) return "null";
  if (typeof props.data === "string") return `"${props.data}"`;
  return String(props.data);
});

function toggle(): void {
  collapsed.value = !collapsed.value;
}
</script>

<template>
  <div
    class="json-view"
    :style="{ paddingLeft: `${depth * 12}px` }"
  >
    <template v-if="isObject && depth < maxDepth">
      <span
        class="json-view__toggle"
        @click="toggle"
      >
        {{ collapsed ? "+" : "-" }}
        {{ isArray ? "[" + entries.length + "]" : "{" + entries.length + "}" }}
      </span>
      <div
        v-if="!collapsed"
        class="json-view__children"
      >
        <div
          v-for="[key, value] in entries"
          :key="key"
          class="json-view__entry"
        >
          <span class="json-view__key">{{ key }}:</span>
          <JsonView
            :data="value"
            :depth="depth + 1"
            :max-depth="maxDepth"
          />
        </div>
      </div>
    </template>
    <template v-else-if="isObject && depth >= maxDepth">
      <span class="json-view__ellipsis">{{ isArray ? "[...]" : "{...}" }}</span>
    </template>
    <template v-else>
      <span
        class="json-view__value"
        :class="{
          'json-view__value--string': typeof data === 'string',
          'json-view__value--number': typeof data === 'number',
          'json-view__value--boolean': typeof data === 'boolean',
          'json-view__value--null': data === null,
        }"
      >{{ displayValue }}</span>
    </template>
  </div>
</template>

<style scoped>
.json-view {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.6;
}

.json-view__toggle {
  cursor: pointer;
  color: var(--text-muted);
  user-select: none;
}

.json-view__toggle:hover {
  color: var(--text-primary);
}

.json-view__children {
  border-left: 1px solid var(--border);
  margin-left: 4px;
}

.json-view__entry {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.json-view__key {
  color: var(--accent);
}

.json-view__value--string {
  color: green;
}

.json-view__value--number {
  color: blue;
}

.json-view__value--boolean {
  color: orange;
}

.json-view__value--null {
  color: var(--text-muted);
  font-style: italic;
}

.json-view__ellipsis {
  color: var(--text-muted);
}
</style>
