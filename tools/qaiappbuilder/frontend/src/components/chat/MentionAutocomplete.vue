<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  MentionAutocomplete.vue — `@<name>` popover for the chat composer.

  Pure presentation: ALL logic lives in :composable:`useMentionAutocomplete`.
  This component just renders the candidates above the textarea and emits
  `pick(name)` when the user clicks a row (Enter / arrow keys are handled by
  the host so they integrate cleanly with the existing composer keybindings).

  Renders ONLY when :prop:`open` is true AND there is at least one candidate;
  hiding the popover when empty avoids a "blank rectangle" between key strokes.
-->
<script setup lang="ts">
import type { MentionCandidate } from "@/composables/chat/useMentionAutocomplete";

defineProps<{
  /** Whether the popover should be visible. */
  open: boolean;
  /** Filtered, ordered candidate list (already excluding used names). */
  candidates: readonly MentionCandidate[];
  /** Currently-highlighted row (0-based). */
  activeIndex: number;
}>();

const emit = defineEmits<{
  /** User clicked a candidate — host commits the insertion. */
  (e: "pick", index: number): void;
  /** User hovered a candidate — host updates the active index. */
  (e: "hover", index: number): void;
}>();

function onClick(idx: number, ev: MouseEvent): void {
  // Prevent the textarea from losing focus mid-click — otherwise the
  // composer's blur handler closes the popover before the click fires.
  ev.preventDefault();
  ev.stopPropagation();
  emit("pick", idx);
}
</script>

<template>
  <div
    v-if="open && candidates.length > 0"
    class="mention-popover"
    data-testid="mention-autocomplete"
    role="listbox"
    @mousedown.prevent
  >
    <button
      v-for="(c, idx) in candidates"
      :key="c.name"
      type="button"
      class="mention-popover-item"
      :class="{ 'is-active': idx === activeIndex }"
      :data-testid="`mention-candidate-${c.name}`"
      role="option"
      :aria-selected="idx === activeIndex"
      @mousedown="onClick(idx, $event)"
      @mouseenter="emit('hover', idx)"
    >
      <span
        class="mention-popover-name"
        :style="c.color ? { color: c.color } : undefined"
      >@{{ c.name }}</span>
      <span
        v-if="c.modelId"
        class="mention-popover-model"
      >· {{ c.modelId }}</span>
    </button>
  </div>
</template>

<style scoped>
.mention-popover {
  position: absolute;
  bottom: 100%;
  left: 0;
  margin-bottom: var(--space-2, 8px);
  min-width: 200px;
  max-width: 360px;
  max-height: 260px;
  overflow-y: auto;
  padding: var(--space-1, 4px);
  background: var(--bg-secondary, #1a1a1a);
  border: 1px solid var(--border, #333);
  border-radius: var(--radius, 6px);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.32);
  z-index: 50;
}

.mention-popover-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  width: 100%;
  padding: 6px 10px;
  background: transparent;
  border: none;
  border-radius: var(--radius-sm, 4px);
  color: var(--text-primary, #eee);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background-color var(--transition, 120ms);
}

.mention-popover-item:hover,
.mention-popover-item.is-active {
  background: var(--bg-hover, color-mix(in srgb, var(--accent, #7c5cff) 16%, transparent));
}

.mention-popover-name {
  font-weight: 500;
}

.mention-popover-model {
  color: var(--text-muted, #888);
  font-size: 0.88em;
  font-variant-numeric: tabular-nums;
}
</style>
