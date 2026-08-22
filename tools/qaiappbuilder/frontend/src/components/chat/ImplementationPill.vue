<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ImplementationPill — compact floating pill for the implementation plan.
 *
 * Positioned in the top-right of `.chat-view` BELOW the TaskListBar dropdown
 * stack (reads `--qai-task-stack-bottom`). z-index 25 sits below queue-float
 * (40) so the transient queue overlay wins when both appear simultaneously.
 * The queue is ephemeral (<2s), so the overlap is acceptable.
 *
 * States:
 * - phase === "none" && isDiscussion && implementationEnabled → "Generate Plan" CTA pill
 * - phase === "planning" → spinner "Planning…"
 * - phase === "planned" / "implementing" / "paused" / "failed" / "completed" → progress pill
 * - otherwise → hidden
 *
 * Click opens the ImplementationDrawer (emits `open-drawer`).
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { useImplementation } from "@/composables/chat/useImplementation";
import { useDiscussion } from "@/composables/chat/useDiscussion";
import { useChatTabsStore } from "@/stores/chatTabs";

const { t } = useI18n();
const impl = useImplementation();
const discussion = useDiscussion();
const store = useChatTabsStore();

const phase = impl.phase;
const items = impl.items;

const emit = defineEmits<{
  (e: "open-drawer"): void;
}>();

/** Minimum number of distinct speakers required before showing Generate. */
const MIN_SPEAKERS = 2;
/** Minimum number of discussion turns (assistant messages with a sender). */
const MIN_DISCUSSION_TURNS = 3;

/** Whether the pill should be visible at all. */
const visible = computed(() => {
  // NEVER show outside of discussion mode.
  const cfg = discussion.config.value;
  if (cfg?.isDiscussion !== true || cfg?.implementationEnabled !== true) return false;

  const p = phase.value;
  // Show when there's already a plan (any non-none phase)
  if (p !== "none") return true;

  // For the "Generate Plan" CTA, the discussion must have progressed enough:
  // 1. Participants with a model configured (planner needs a model to call)
  const roster = discussion.participants.value;
  const hasModeledParticipant = roster.length > 0 && roster.some(
    (r) => r.model_id !== undefined && r.model_id !== "",
  );
  if (!hasModeledParticipant) return false;

  // 2. Multiple speakers have actually spoken (real multi-agent discussion)
  const tab = store.activeTab;
  if (tab === null) return false;
  const speakerIds = new Set<string>();
  let discussionTurns = 0;
  for (const msg of tab.messages) {
    if (msg.role === "assistant" && msg.senderId) {
      speakerIds.add(msg.senderId);
      discussionTurns++;
    }
  }
  return speakerIds.size >= MIN_SPEAKERS && discussionTurns >= MIN_DISCUSSION_TURNS;
});

/** Done / total counts for progress display. */
const doneCount = computed(
  () => items.value.filter((x) => x.status === "done").length,
);
const totalCount = computed(() => items.value.length);

/** The compact label shown in the pill. */
const pillLabel = computed(() => {
  const p = phase.value;
  if (p === "none") return t("chat.implementation.controls.generate");
  if (p === "planning") return t("chat.implementation.phaseLabel.planning");
  if (p === "planning_failed")
    return t("chat.implementation.phaseLabel.planning_failed");
  // For phases with items, show "done/total"
  return `${doneCount.value}/${totalCount.value}`;
});

/** CSS modifier class based on phase. */
const pillClass = computed(() => {
  const p = phase.value;
  if (p === "completed") return "impl-pill--done";
  if (p === "failed" || p === "planning_failed") return "impl-pill--error";
  if (p === "implementing") return "impl-pill--active";
  if (p === "paused") return "impl-pill--paused";
  if (p === "none") return "impl-pill--generate";
  return "";
});

/** Whether to show the spinning indicator. */
const showSpinner = computed(() => {
  const p = phase.value;
  return p === "planning" || p === "implementing";
});
</script>

<template>
  <div v-if="visible" class="impl-pill-anchor" data-testid="impl-pill-anchor">
    <button
      type="button"
      class="impl-pill"
      :class="pillClass"
      :title="t('chat.implementation.title')"
      data-testid="impl-pill"
      @click="emit('open-drawer')"
    >
      <!-- Icon: clipboard / plan -->
      <svg
        class="impl-pill-icon"
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path
          d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"
        />
        <rect x="8" y="2" width="8" height="4" rx="1" ry="1" />
      </svg>
      <!-- Spinner for active states -->
      <span v-if="showSpinner" class="impl-pill-spinner" aria-hidden="true" />
      <span class="impl-pill-label">{{ pillLabel }}</span>
    </button>
  </div>
</template>

<style scoped>
.impl-pill-anchor {
  position: absolute;
  /* Right column, stacks below TaskListBar + dropdown (reads the published
     --qai-task-stack-bottom). z-index 25 sits below queue-float (40) so the
     transient message queue overlays the pill when both appear. When neither
     the task-bar nor queue-float exist, falls back to top:12px right:12px. */
  top: calc(
    var(--qai-task-stack-bottom, 0px) +
    var(--qai-mbp-iframe-bottom, 0px) +
    var(--space-3, 12px)
  );
  right: var(--space-3, 12px);
  z-index: 25;
  pointer-events: auto;
  transition: top 0.2s ease;
}

.impl-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full, 999px);
  background: var(--bg-tertiary, #2a2a2a);
  color: var(--text-secondary, #aaa);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
  white-space: nowrap;
  line-height: 1;
}
.impl-pill:hover {
  background: var(--bg-hover, #333);
  border-color: var(--accent, #5b9bd5);
  color: var(--text-primary, #eee);
}

/* -- State modifiers -- */
.impl-pill--generate {
  border-color: var(--accent, #5b9bd5);
  border-width: 1.5px;
  color: var(--accent, #5b9bd5);
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-tertiary, #2a2a2a));
  box-shadow: 0 0 6px color-mix(in srgb, var(--accent) 25%, transparent);
}
.impl-pill--generate:hover {
  background: color-mix(in srgb, var(--accent) 18%, var(--bg-tertiary, #2a2a2a));
  box-shadow: 0 0 10px color-mix(in srgb, var(--accent) 35%, transparent);
}
.impl-pill--active {
  border-color: var(--accent, #5b9bd5);
  color: var(--accent, #5b9bd5);
}
.impl-pill--paused {
  border-color: var(--text-muted, #666);
  color: var(--text-muted, #666);
}
.impl-pill--done {
  border-color: var(--success, #4caf50);
  color: var(--success, #4caf50);
}
.impl-pill--error {
  border-color: var(--error, #e57373);
  color: var(--error, #e57373);
}

/* -- Icon -- */
.impl-pill-icon {
  flex-shrink: 0;
  opacity: 0.8;
}

/* -- Spinner (pulsing dot) -- */
.impl-pill-spinner {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  animation: impl-pulse 1.2s ease-in-out infinite;
}
@keyframes impl-pulse {
  0%,
  100% {
    opacity: 0.3;
    transform: scale(0.85);
  }
  50% {
    opacity: 1;
    transform: scale(1.1);
  }
}

/* -- Label -- */
.impl-pill-label {
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 120px;
}
</style>
