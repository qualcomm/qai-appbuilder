<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ToolGroupToggle — Shared component for displaying tools organized by
 * permission groups with one-click group toggle buttons.
 *
 * Supports two semantic modes via `mode` prop:
 * - "allowed" (whitelist): checked = tool IS in the list (AgentRoleForm)
 * - "disabled" (blacklist): checked = tool is NOT in the list (SessionToolsPopover)
 *
 * Emits `update:modelValue` with the updated list (string[]).
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";

// ─── Props & Emits ───────────────────────────────────────────────────────────

interface Props {
  /** The current list of tool names (allowed or disabled, depending on mode). */
  modelValue: string[];
  /** All available tool names (the full catalog). */
  tools: string[];
  /** Semantic mode: "allowed" = whitelist, "disabled" = blacklist. */
  mode?: "allowed" | "disabled";
  /**
   * Optional chip-dimming (§7.16 / AgentRoleForm decision-2): when a tool is
   * "dimmed" its chip is greyed + tooltip'd but stays selectable (the tool
   * becomes live again under a permissive mode). This is an ORTHOGONAL visual
   * hint driven by the host's mode policy — it does NOT change the
   * whitelist/blacklist toggle semantics. Callers that don't pass a predicate
   * (e.g. SessionToolsPopover) keep the exact prior behaviour (no dimming).
   */
  isDimmed?: (tool: string) => boolean;
  /** Optional tooltip text for a dimmed tool chip (empty ⇒ no title). */
  dimTooltip?: (tool: string) => string;
}

const props = withDefaults(defineProps<Props>(), {
  mode: "allowed",
  isDimmed: undefined,
  dimTooltip: undefined,
});

/** Is a given tool dimmed? Safe wrapper — no predicate ⇒ never dimmed. */
function chipDimmed(tool: string): boolean {
  return props.isDimmed ? props.isDimmed(tool) : false;
}

/** Tooltip for a tool chip — empty string when not dimmed / no resolver. */
function chipTitle(tool: string): string {
  if (!chipDimmed(tool)) return "";
  return props.dimTooltip ? props.dimTooltip(tool) : "";
}

const emit = defineEmits<{
  "update:modelValue": [value: string[]];
}>();

// ─── i18n ────────────────────────────────────────────────────────────────────

const { t } = useI18n();

// ─── Group Definitions ───────────────────────────────────────────────────────

interface ToolGroup {
  id: string;
  labelKey: string;
  members: Set<string>;
}

const GROUPS: ToolGroup[] = [
  {
    id: "read",
    labelKey: "codePersona.groups.read",
    members: new Set(["read", "glob", "grep", "webfetch", "web_search", "list"]),
  },
  {
    id: "edit",
    labelKey: "codePersona.groups.edit",
    members: new Set(["edit", "write", "apply_patch"]),
  },
  {
    id: "command",
    labelKey: "codePersona.groups.command",
    members: new Set(["exec", "background_process"]),
  },
];

// Tools that don't belong to any group (always shown ungrouped).
const ungroupedTools = computed(() => {
  const grouped = new Set<string>();
  for (const g of GROUPS) {
    for (const m of g.members) grouped.add(m);
  }
  return props.tools.filter((t) => !grouped.has(t));
});

// ─── State Helpers ───────────────────────────────────────────────────────────

/** Is a given tool currently "on" (checked in the UI)? */
function isOn(tool: string): boolean {
  if (props.mode === "disabled") {
    // "disabled" mode: tool is ON when it's NOT in the disabled list.
    return !props.modelValue.includes(tool);
  }
  // "allowed" mode: tool is ON when it IS in the allowed list.
  return props.modelValue.includes(tool);
}

/** Toggle a single tool. */
function toggle(tool: string): void {
  const list = [...props.modelValue];
  const idx = list.indexOf(tool);
  if (props.mode === "disabled") {
    // Toggle: if in disabled list, remove (turn ON); if not, add (turn OFF).
    if (idx >= 0) list.splice(idx, 1);
    else list.push(tool);
  } else {
    // Toggle: if in allowed list, remove (turn OFF); if not, add (turn ON).
    if (idx >= 0) list.splice(idx, 1);
    else list.push(tool);
  }
  emit("update:modelValue", list);
}

/** Is an entire group fully "on"? */
function isGroupOn(group: ToolGroup): boolean {
  const members = props.tools.filter((t) => group.members.has(t));
  return members.length > 0 && members.every((t) => isOn(t));
}

/** Is an entire group partially "on"? */
function isGroupPartial(group: ToolGroup): boolean {
  const members = props.tools.filter((t) => group.members.has(t));
  const onCount = members.filter((t) => isOn(t)).length;
  return onCount > 0 && onCount < members.length;
}

/** Toggle entire group on/off. */
function toggleGroup(group: ToolGroup): void {
  const members = props.tools.filter((t) => group.members.has(t));
  const allOn = isGroupOn(group);
  let list = [...props.modelValue];

  if (props.mode === "disabled") {
    if (allOn) {
      // Turn group OFF = add all members to disabled list.
      for (const m of members) {
        if (!list.includes(m)) list.push(m);
      }
    } else {
      // Turn group ON = remove all members from disabled list.
      list = list.filter((t) => !group.members.has(t));
    }
  } else {
    if (allOn) {
      // Turn group OFF = remove all members from allowed list.
      list = list.filter((t) => !group.members.has(t));
    } else {
      // Turn group ON = add all members to allowed list.
      for (const m of members) {
        if (!list.includes(m)) list.push(m);
      }
    }
  }
  emit("update:modelValue", list);
}

/** Tools in a group that are actually present in the catalog. */
function groupTools(group: ToolGroup): string[] {
  return props.tools.filter((t) => group.members.has(t));
}
</script>

<template>
  <div class="tool-group-toggle">
    <!-- Grouped tools -->
    <div
      v-for="group in GROUPS"
      :key="group.id"
      class="tgt-section"
    >
      <div
        class="tgt-section-header"
        @click="toggleGroup(group)"
      >
        <span class="tgt-section-check" :class="{ 'is-on': isGroupOn(group), 'is-partial': isGroupPartial(group) }">
          <template v-if="isGroupOn(group)">&#x2714;</template>
          <template v-else-if="isGroupPartial(group)">&#x2500;</template>
        </span>
        <span class="tgt-section-label">{{ t(group.labelKey) }}</span>
      </div>
      <div class="tgt-section-tools">
        <label
          v-for="tool in groupTools(group)"
          :key="tool"
          class="tgt-chip"
          :class="{ 'is-on': isOn(tool), 'is-dimmed': chipDimmed(tool) }"
          :title="chipTitle(tool)"
        >
          <input
            type="checkbox"
            :checked="isOn(tool)"
            :data-testid="`role-form-tool-${tool}`"
            @change="toggle(tool)"
          />
          {{ tool }}
        </label>
      </div>
    </div>

    <!-- Ungrouped tools (always-available or uncategorized) -->
    <div
      v-if="ungroupedTools.length > 0"
      class="tgt-section"
    >
      <div class="tgt-section-header tgt-section-header--static">
        <span class="tgt-section-label">{{ t("codePersona.groups.other", "其它") }}</span>
      </div>
      <div class="tgt-section-tools">
        <label
          v-for="tool in ungroupedTools"
          :key="tool"
          class="tgt-chip"
          :class="{ 'is-on': isOn(tool), 'is-dimmed': chipDimmed(tool) }"
          :title="chipTitle(tool)"
        >
          <input
            type="checkbox"
            :checked="isOn(tool)"
            :data-testid="`role-form-tool-${tool}`"
            @change="toggle(tool)"
          />
          {{ tool }}
        </label>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tool-group-toggle {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.tgt-section {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 0;
  border-bottom: 1px solid var(--border-default);
}
.tgt-section:last-child {
  border-bottom: none;
}
.tgt-section-header {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
  padding: 2px 4px;
  border-radius: 4px;
  transition: background 0.15s;
}
.tgt-section-header:hover {
  background: var(--bg-hover, rgba(255, 255, 255, 0.05));
}
.tgt-section-header--static {
  cursor: default;
}
.tgt-section-header--static:hover {
  background: transparent;
}
.tgt-section-check {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 3px;
  border: 1.5px solid var(--text-muted, #888);
  font-size: 10px;
  color: var(--text-muted);
  background: transparent;
  transition: all 0.15s;
}
.tgt-section-check.is-on {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--text-on-accent, #fff);
}
.tgt-section-check.is-partial {
  border-color: var(--accent);
  color: var(--accent);
}
.tgt-section-label {
  font-size: var(--text-xs);
  color: var(--text-muted);
  font-weight: 500;
}
.tgt-section-tools {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding-left: 22px;
}
.tgt-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: var(--text-xs);
  border: 1px solid var(--border-default);
  cursor: pointer;
  color: var(--text-muted);
  transition: all 0.15s;
}
.tgt-chip:hover {
  border-color: var(--accent);
}
.tgt-chip.is-on {
  background: var(--accent);
  color: var(--text-on-accent, #fff);
  border-color: var(--accent);
}
/* Chip-dimming (§7.16 / AgentRoleForm decision-2): a tool disabled by the
   current mode policy is greyed but still selectable. Orthogonal to `is-on`
   (a dimmed chip may still be checked). Mirrors the pre-refactor
   `.role-tool-chip.is-dimmed` rule. */
.tgt-chip.is-dimmed {
  opacity: 0.45;
}
.tgt-chip input[type="checkbox"] {
  width: 12px;
  height: 12px;
  cursor: pointer;
}
</style>
