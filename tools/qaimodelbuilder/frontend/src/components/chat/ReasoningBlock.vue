<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ReasoningBlock — collapsible "思考过程" block for a chat assistant turn.
 *
 * Renders the model's *thinking* (reasoning) text in a foldable panel ABOVE the
 * answer bubble, visually distinct from the final answer. Fed by `reasoning`
 * stream frames (cloud reasoning models' `delta.reasoning_content`, previously
 * discarded, + the internal query-service adapter's noise-filtered thinking),
 * accumulated onto `ChatMessage.reasoning`.
 *
 * Interaction mirrors the project's existing collapsible cards
 * (`CollapsibleCard.vue` / `SubAgentBlock.vue`): a clickable header toggles a ▼
 * arrow + `v-show` body, keyboard-accessible (enter/space). **Default open**
 * (per product decision) so the thinking is visible by default, consistent with
 * the current chat experience. The title reuses the existing `chat.thinking`
 * i18n key ("思考中..." / "Thinking...") — no new i18n key. The reasoning text is
 * rendered through the same Markdown pipeline as the answer for visual
 * consistency.
 */
import { computed, ref, toRef } from "vue";
import { useI18n } from "vue-i18n";

import { renderMarkdown } from "@/composables/markdown";
import { useMermaidRender } from "@/composables/useMermaidRender";

const props = withDefaults(
  defineProps<{
    /** Accumulated reasoning text for this turn. */
    text: string;
    /** Whether the block starts expanded (product default: true). */
    defaultOpen?: boolean;
  }>(),
  { defaultOpen: true },
);

const { t } = useI18n();
const open = ref(props.defaultOpen);

const renderedHtml = computed(() => renderMarkdown(props.text));

// Native Mermaid rendering for ```mermaid``` blocks inside reasoning. The hook
// runs after the v-html flush; re-renders on text change (idempotent) + theme
// switch. `open` gates it so a collapsed body (display:none) still renders once
// reopened (the watch on `open` reschedules).
const bodyEl = ref<HTMLElement | null>(null);
useMermaidRender(bodyEl, {
  content: [toRef(props, "text"), open],
  labels: () => ({
    rendering: t("chat.mermaid.rendering"),
    renderError: (message: string) => t("chat.mermaid.renderError", { message }),
    errorDefault: t("chat.mermaid.errorDefault"),
    errorEmpty: t("chat.mermaid.errorEmpty"),
  }),
});
</script>

<template>
  <section
    v-if="text !== ''"
    class="reasoning-block"
    data-testid="reasoning-block"
  >
    <header
      class="reasoning-block__header"
      role="button"
      tabindex="0"
      :aria-expanded="open"
      @click="open = !open"
      @keydown.enter.prevent="open = !open"
      @keydown.space.prevent="open = !open"
    >
      <span class="reasoning-block__icon" aria-hidden="true">🤔</span>
      <span class="reasoning-block__title">{{ t("chat.thinking") }}</span>
      <span
        class="reasoning-block__arrow"
        :class="{ 'reasoning-block__arrow--collapsed': !open }"
        aria-hidden="true"
      >▼</span>
    </header>
    <div
      v-show="open"
      ref="bodyEl"
      class="reasoning-block__body markdown-body"
      v-html="renderedHtml"
    />
  </section>
</template>

<style scoped>
.reasoning-block {
  margin: 0 0 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface-muted, rgba(127, 127, 127, 0.06));
  overflow: hidden;
}

.reasoning-block__header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  cursor: pointer;
  user-select: none;
  font-size: 0.85em;
  color: var(--text-secondary, rgba(127, 127, 127, 0.9));
}

.reasoning-block__header:hover {
  background: var(--surface-hover, rgba(127, 127, 127, 0.1));
}

.reasoning-block__icon {
  font-size: 1em;
  line-height: 1;
}

.reasoning-block__title {
  flex: 1 1 auto;
  font-weight: 500;
}

.reasoning-block__arrow {
  flex: 0 0 auto;
  transition: transform 0.15s ease;
  font-size: 0.75em;
}

.reasoning-block__arrow--collapsed {
  transform: rotate(-90deg);
}

.reasoning-block__body {
  padding: 4px 12px 10px;
  font-size: 0.88em;
  color: var(--text-secondary, rgba(127, 127, 127, 0.95));
  border-top: 1px solid var(--border);
}
</style>
