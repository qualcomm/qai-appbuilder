<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ToolPermissionMatrix — clickable tri/quad-state tool permission grid.
 *
 * Generic over the state set so it serves both providers:
 *   - Claude Code: 3 states  default → allowed → disallowed
 *   - OpenCode:    4 states  default → allow → ask → deny
 *
 * The parent owns the source-of-truth (CC: two arrays; OC: a dict) and
 * passes a `stateOf(toolId)` resolver plus the ordered `states`. Each
 * click emits `cycle(toolId, nextStateId)`; the parent applies it.
 */
import { computed } from "vue";

interface ToolDef {
  id: string;
  desc?: string;
}

interface StateDef {
  /** state id, e.g. "default" | "allowed" | "disallowed" | "allow" | "ask" | "deny" */
  id: string;
  /** short label shown on the tag */
  label: string;
  /** glyph shown before the label */
  icon: string;
  /** css color for active border/text */
  color: string;
}

const props = defineProps<{
  tools: ToolDef[];
  states: StateDef[];
  /** resolves the current state id for a given tool */
  stateOf: (toolId: string) => string;
}>();

const emit = defineEmits<{
  (e: "cycle", toolId: string, nextStateId: string): void;
}>();

const stateById = computed<Record<string, StateDef>>(() => {
  const map: Record<string, StateDef> = {};
  for (const s of props.states) map[s.id] = s;
  return map;
});

function nextStateId(current: string): string {
  const ids = props.states.map((s) => s.id);
  const idx = ids.indexOf(current);
  // Unknown state (e.g. OC fine-grained object) → cycle back to first.
  if (idx === -1) return ids[0] ?? "default";
  return ids[(idx + 1) % ids.length] ?? ids[0] ?? "default";
}

function onClick(toolId: string): void {
  emit("cycle", toolId, nextStateId(props.stateOf(toolId)));
}

function tagStyle(toolId: string): Record<string, string> {
  const st = stateById.value[props.stateOf(toolId)];
  if (!st || st.id === props.states[0]?.id) {
    return {
      background: "var(--bg-tertiary, #1a2a3a)",
      borderColor: "var(--border, #2a3a4a)",
      color: "var(--text-secondary)",
    };
  }
  return {
    background: "color-mix(in srgb, " + st.color + " 15%, transparent)",
    borderColor: st.color,
    color: st.color,
  };
}
</script>

<template>
  <div class="qai-tool-matrix">
    <div
      v-for="tool in tools"
      :key="tool.id"
      class="qai-tool-matrix__tag"
      :style="tagStyle(tool.id)"
      :title="tool.desc"
      data-testid="tool-permission-tag"
      @click="onClick(tool.id)"
    >
      <span class="qai-tool-matrix__icon">{{
        stateById[stateOf(tool.id)]?.icon ?? "○"
      }}</span>
      <span class="qai-tool-matrix__id">{{ tool.id }}</span>
      <span class="qai-tool-matrix__state">{{
        stateById[stateOf(tool.id)]?.label ?? stateOf(tool.id)
      }}</span>
    </div>
  </div>
</template>

<style scoped>
.qai-tool-matrix {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.qai-tool-matrix__tag {
  cursor: pointer;
  user-select: none;
  border-radius: 6px;
  padding: 5px 12px;
  font-size: var(--text-base, 0.9rem);
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 6px;
  transition: all 0.15s;
  border: 1.5px solid;
}

.qai-tool-matrix__icon {
  font-size: var(--text-xs, 0.75rem);
  line-height: 1;
}

.qai-tool-matrix__state {
  font-size: var(--text-xs, 0.75rem);
  opacity: 0.75;
  font-weight: 400;
}
</style>
