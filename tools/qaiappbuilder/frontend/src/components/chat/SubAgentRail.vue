<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * SubAgentRail — horizontal navigation strip rendered DIRECTLY BELOW the
 * active parent main-agent tab (which lives in the top ChatTabStrip). Restores
 * the two-level tab bar UX after β incorrectly flattened everything into a
 * single tab strip.
 *
 * Structure
 *   ┌─ accent bar (4px, left edge)                                        ┐
 *   │ [⌂ Main] │ [● Sub A] [● Sub B] … [○ Sub Z (closed)] [⋯]              │
 *   └────────────────────────────────────────────────────────────────────┘
 *
 * Data model (mixed source; see ChatView.railEntries):
 *   - Backend `SubAgentSession` list (via `state.subAgentIndex[rootConvId]`)
 *     is the SUPERSET: every sub-agent EVER spawned under this main tab's
 *     conversation, whether its tab is currently open or not.
 *   - `state.tabs`'s open sub-agent tabs supply LIVE status (running /
 *     aborting / streaming) that override the persisted status from the
 *     backend for that entry.
 *   - Closed sub-agents (no open tab) still render a chip — greyed via
 *     `--offline` — so the user can one-click re-hydrate via
 *     `openSubAgentTab(sid)`. Closing the × on an open chip = memory unload,
 *     NOT a delete (backend session is preserved).
 *
 * Component contract: PURE props/emits (no store access) so it stays trivially
 * unit-testable and swappable into any parent layout. The owning `ChatView` is
 * responsible for wiring the emits to `chatTabs` actions.
 */
import { computed, nextTick, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import ChatOverflowPopover from "@/components/chat/ChatOverflowPopover.vue";
import { useHorizontalOverflow } from "@/composables/chat/useHorizontalOverflow";

/** One row rendered by the rail. The caller merges the persisted backend
 *  index with the open-tab live status; see ChatView.railEntries. */
export interface RailEntry {
  readonly subagentId: string;
  readonly title: string;
  /** Effective status — live "running" / "aborting" from an open tab if any,
   *  else the last-known persisted status from the backend index. */
  readonly status: string;
  /** True when this sub-agent has an open tab in `state.tabs` (chip renders
   *  in the normal / lit state); false when only the index entry exists (chip
   *  renders greyed to signal "click to re-hydrate"). */
  readonly isOpen: boolean;
  /** Recursion depth (1 = first-level, 2 = grand, 3 = great-grand, ...).
   *  Not rendered as a visible badge — kept for future filtering / a11y
   *  order announcements. */
  readonly depth: number;
}

interface Props {
  /** Sub-agents rooted at the active main tab (open + closed). Empty array
   *  → rail collapses via CSS `.sub-agent-rail--empty { max-height: 0 }`. */
  subAgents: readonly RailEntry[];
  /** Active sub-agent id; `null` means the MAIN conversation is focused. */
  activeSubAgentId: string | null;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  /** User picked the leading "⌂ Main" button. */
  (e: "select-main"): void;
  /** User picked a sub-agent chip (or an overflow popover row). Includes
   *  closed chips — the caller runs `openSubAgentTab(sid)` which hydrates. */
  (e: "select-subagent", subagentId: string): void;
  /** User clicked the `×` close affordance on a chip. The caller closes the
   *  open sub-agent tab (memory cleanup); the chip stays visible (as `isOpen:
   *  false`) so the user can re-hydrate later. */
  (e: "close-subagent", subagentId: string): void;
}>();

const { t } = useI18n();

// ── Derived state ────────────────────────────────────────────────────────────
const totalCount = computed(() => props.subAgents.length);
const isEmpty = computed(() => totalCount.value === 0);
const isMainActive = computed(() => props.activeSubAgentId === null);

/** Strip a leading "SubAgent:" prefix (case-insensitive) so the rail stays
 *  compact — sub-agent tab titles carry the prefix as a legacy disambiguation
 *  cue that's redundant inside a rail labelled "Sub-agent navigation". */
function displayTitle(entry: RailEntry): string {
  const raw = (entry.title ?? "").trim();
  const stripped = raw.replace(/^sub[- ]?agent\s*:\s*/i, "");
  return stripped.length > 0 ? stripped : entry.subagentId;
}

/** Normalize the free-form status string into one of the 5 visual buckets
 *  (idle / running / aborting / error / done). Anything unrecognised → idle. */
function statusBucket(
  status: string | undefined,
): "running" | "aborting" | "error" | "done" | "idle" {
  switch ((status ?? "").toLowerCase()) {
    case "running":
    case "streaming":
      return "running";
    case "aborting":
    case "cancelling":
    case "canceling":
      return "aborting";
    case "error":
    case "failed":
      return "error";
    case "done":
    case "finished":
    case "complete":
    case "completed":
      return "done";
    default:
      return "idle";
  }
}

function statusLabel(status: string | undefined): string {
  const bucket = statusBucket(status);
  switch (bucket) {
    case "running":
      return t("chat.subAgent.rail.statusRunning");
    case "aborting":
      return t("chat.subAgent.rail.statusAborting");
    case "error":
      return t("chat.subAgent.rail.statusError");
    case "done":
      return t("chat.subAgent.rail.statusDone");
    default:
      return t("chat.subAgent.rail.statusIdle");
  }
}

// ── Event wiring ─────────────────────────────────────────────────────────────
function onSelectMain(): void {
  emit("select-main");
}

function onSelectSubagent(entry: RailEntry): void {
  emit("select-subagent", entry.subagentId);
}

function onCloseSubagent(entry: RailEntry, ev: Event): void {
  // The chip itself owns a `click` handler that selects the sub-agent;
  // closing must NOT also activate it.
  ev.stopPropagation();
  emit("close-subagent", entry.subagentId);
}

// ── Shared horizontal-overflow mechanics (parity with ChatTabStrip) ─────────
// MEASURED overflow (real `scrollWidth - clientWidth` probe), wheel→scroll
// translation, edge fades, ResizeObserver, popover open/close — all provided
// by the composable. Outside-click dismissal is owned by ChatOverflowPopover.
const {
  scrollEl,
  isOverflowing,
  canScrollLeft,
  canScrollRight,
  overflowOpen,
  toggleOverflow,
  closeOverflow,
  recompute,
} = useHorizontalOverflow();

// Trigger `<button>` ref → `ChatOverflowPopover` (viewport-rect positioning
// + outside-click trigger whitelist). Popover must be Teleported (not
// inline) because the rail's `overflow: hidden` (for the `--empty` collapse
// animation) would clip an inline popover.
const overflowTriggerEl = ref<HTMLElement | null>(null);

function pickFromOverflow(entry: RailEntry): void {
  emit("select-subagent", entry.subagentId);
  closeOverflow();
}

// Re-measure when sub-agent count / titles / active id / open-state change
// (any of which can shift the rail's natural width and thus overflow + fade
// state).
watch(
  () =>
    props.subAgents
      .map((r) => `${r.subagentId}:${r.title}:${r.isOpen ? "1" : "0"}`)
      .join("|") + `#${props.activeSubAgentId ?? ""}`,
  () => void nextTick(recompute),
);
</script>

<template>
  <div
    class="sub-agent-rail"
    :class="{
      'sub-agent-rail--empty': isEmpty,
      'sub-agent-rail--fade-left': canScrollLeft,
      'sub-agent-rail--fade-right': canScrollRight,
    }"
    role="tablist"
    :aria-label="t('chat.subAgent.rail.label')"
    data-testid="sub-agent-rail"
  >
    <!-- Left accent bar — visual cue that this strip is a SECOND-level
         navigator subordinate to the top tab strip. -->
    <span
      class="sub-agent-rail__accent"
      aria-hidden="true"
    />

    <div
      ref="scrollEl"
      class="sub-agent-rail__container"
    >
      <!-- "⌂ Main" button — always first, navigates back to the parent
           main-agent conversation. -->
      <button
        type="button"
        role="tab"
        class="sub-agent-rail__main-btn"
        :class="{
          'sub-agent-rail__main-btn--active': isMainActive,
        }"
        :aria-selected="isMainActive ? 'true' : 'false'"
        :tabindex="isMainActive ? 0 : -1"
        :title="t('chat.subAgent.rail.mainTooltip')"
        data-testid="sub-agent-rail-main"
        @click="onSelectMain"
      >
        <svg
          class="sub-agent-rail__main-icon"
          viewBox="0 0 16 16"
          width="12"
          height="12"
          aria-hidden="true"
          focusable="false"
        >
          <path
            fill="currentColor"
            d="M8 1.5 1.5 7v7.5h4V10h5v4.5h4V7L8 1.5Z"
          />
        </svg>
        <span class="sub-agent-rail__main-label">{{
          t("chat.subAgent.rail.main")
        }}</span>
      </button>

      <!-- Divider between "Main" and the sub-agent chips. -->
      <span
        v-if="totalCount > 0"
        class="sub-agent-rail__spacer"
        aria-hidden="true"
      />

      <!-- Sub-agent chips — ALL rendered (open + closed). Closed chips
           carry the `--offline` modifier so the user visually distinguishes
           "loaded in memory" from "just a persisted reference"; a click on
           a closed chip triggers a fresh hydrate. -->
      <button
        v-for="entry in subAgents"
        :key="entry.subagentId"
        type="button"
        role="tab"
        class="sub-agent-rail__chip"
        :class="{
          'sub-agent-rail__chip--active':
            entry.subagentId === activeSubAgentId,
          'sub-agent-rail__chip--offline': !entry.isOpen,
        }"
        :aria-selected="
          entry.subagentId === activeSubAgentId ? 'true' : 'false'
        "
        :tabindex="entry.subagentId === activeSubAgentId ? 0 : -1"
        :data-subagent-id="entry.subagentId"
        :data-status="statusBucket(entry.status)"
        :data-is-open="entry.isOpen ? 'true' : 'false'"
        :title="displayTitle(entry)"
        @click="onSelectSubagent(entry)"
      >
        <span
          class="sub-agent-rail__chip-dot"
          :class="[
            `sub-agent-rail__chip-dot--${statusBucket(entry.status)}`,
            { 'sub-agent-rail__chip-dot--offline': !entry.isOpen },
          ]"
          aria-hidden="true"
        />
        <span class="sub-agent-rail__chip-title">{{ displayTitle(entry) }}</span>
        <span
          class="sub-agent-rail__chip-underline"
          aria-hidden="true"
        />
        <!-- Close × — only meaningful for OPEN chips (memory unload). On a
             closed chip the affordance is hidden so it can't be pressed. -->
        <span
          v-if="entry.isOpen"
          class="sub-agent-rail__chip-close"
          role="button"
          tabindex="0"
          :aria-label="t('chat.subAgent.rail.closeSubAgent')"
          :title="t('chat.subAgent.rail.closeSubAgent')"
          data-testid="sub-agent-rail-chip-close"
          @click="(ev) => onCloseSubagent(entry, ev)"
          @keydown.enter.stop.prevent="(ev) => onCloseSubagent(entry, ev)"
          @keydown.space.stop.prevent="(ev) => onCloseSubagent(entry, ev)"
        >
          ×
        </span>
      </button>
    </div>

    <!-- Overflow "⋯" trigger + popover. Panel is Teleported to `<body>` via
         `ChatOverflowPopover` — critical because `.sub-agent-rail`'s
         `overflow: hidden` (needed for the `--empty` collapse animation)
         would otherwise clip any inline popover out of view. -->
    <div
      v-if="isOverflowing"
      class="sub-agent-rail__overflow"
    >
      <button
        ref="overflowTriggerEl"
        type="button"
        class="sub-agent-rail__overflow-trigger"
        :class="{
          'sub-agent-rail__overflow-trigger--open': overflowOpen,
        }"
        :aria-label="
          t('chat.subAgent.rail.overflowMore', { count: totalCount })
        "
        :title="
          t('chat.subAgent.rail.overflowMore', { count: totalCount })
        "
        :aria-expanded="overflowOpen"
        aria-haspopup="menu"
        data-testid="sub-agent-rail-overflow"
        @click="toggleOverflow"
        @keydown.esc="closeOverflow"
      >
        ⋯
      </button>
      <ChatOverflowPopover
        :open="overflowOpen"
        :trigger-el="overflowTriggerEl"
        align="right"
        min-width="240px"
        max-width="360px"
        panel-class="sub-agent-rail__popover"
        :aria-label="t('chat.subAgent.rail.overflowTitle')"
        testid="sub-agent-rail-popover"
        @close="closeOverflow"
      >
        <div
          class="sub-agent-rail__popover-header"
          aria-hidden="true"
        >
          {{ t("chat.subAgent.rail.overflowTitle") }}
        </div>
        <button
          v-for="entry in subAgents"
          :key="entry.subagentId"
          type="button"
          role="menuitem"
          class="sub-agent-rail__popover-item"
          :class="{
            'sub-agent-rail__popover-item--active':
              entry.subagentId === activeSubAgentId,
            'sub-agent-rail__popover-item--offline': !entry.isOpen,
          }"
          :data-subagent-id="entry.subagentId"
          @click="pickFromOverflow(entry)"
        >
          <span
            class="sub-agent-rail__chip-dot"
            :class="[
              `sub-agent-rail__chip-dot--${statusBucket(entry.status)}`,
              { 'sub-agent-rail__chip-dot--offline': !entry.isOpen },
            ]"
            aria-hidden="true"
          />
          <span class="sub-agent-rail__popover-item-title">{{
            displayTitle(entry)
          }}</span>
          <span class="sub-agent-rail__popover-item-status">{{
            statusLabel(entry.status)
          }}</span>
        </button>
      </ChatOverflowPopover>
    </div>
  </div>
</template>

<style scoped>
/* ── Container ──────────────────────────────────────────────────────────────
 * 32px high single row, slim accent bar on the left. The `max-height`
 * transition animates the rail in/out smoothly when the sub-agent set is
 * empty — no jarring layout jump.
 */
.sub-agent-rail {
  position: relative;
  display: flex;
  align-items: stretch;
  width: 100%;
  height: 32px;
  max-height: 32px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
  overflow: hidden;
  transition: max-height 200ms ease;
}

/* No sub-agents → collapse smoothly to 0 height. */
.sub-agent-rail--empty {
  max-height: 0;
  border-bottom-color: transparent;
}

/* Left accent strip — 4px theme accent bar identifying this as a "child of
 * the parent tab" navigator. */
.sub-agent-rail__accent {
  flex: 0 0 4px;
  background: var(--accent);
}

/* Horizontal scroll track. Native scrollbar hidden; wheel-Y is mapped to
 * scroll-X inside the composable. */
.sub-agent-rail__container {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 1 1 auto;
  min-width: 0;
  padding: 0 6px;
  overflow-x: auto;
  scrollbar-width: none;
  -ms-overflow-style: none;
}
.sub-agent-rail__container::-webkit-scrollbar {
  display: none;
}

/* Directional edge fades (parity with ChatTabStrip). */
.sub-agent-rail--fade-right .sub-agent-rail__container {
  -webkit-mask-image: linear-gradient(
    to right,
    #000 calc(100% - 24px),
    transparent 100%
  );
  mask-image: linear-gradient(
    to right,
    #000 calc(100% - 24px),
    transparent 100%
  );
}

.sub-agent-rail--fade-left .sub-agent-rail__container {
  -webkit-mask-image: linear-gradient(to right, transparent 0, #000 24px);
  mask-image: linear-gradient(to right, transparent 0, #000 24px);
}

.sub-agent-rail--fade-left.sub-agent-rail--fade-right
  .sub-agent-rail__container {
  -webkit-mask-image: linear-gradient(
    to right,
    transparent 0,
    #000 24px,
    #000 calc(100% - 24px),
    transparent 100%
  );
  mask-image: linear-gradient(
    to right,
    transparent 0,
    #000 24px,
    #000 calc(100% - 24px),
    transparent 100%
  );
}

/* ── Leading "Main" button ──────────────────────────────────────────────── */
.sub-agent-rail__main-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
  height: 24px;
  padding: 0 8px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 4px;
  cursor: pointer;
  color: var(--text-secondary);
  font: inherit;
  font-size: var(--text-sm);
  line-height: 1;
  user-select: none;
  transition: background-color 80ms ease, color 80ms ease;
}

.sub-agent-rail__main-btn:hover {
  background: var(--bg-hover, rgba(0, 0, 0, 0.04));
  color: var(--text-primary);
}

.sub-agent-rail__main-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.sub-agent-rail__main-btn--active {
  color: var(--text-primary);
  font-weight: 600;
  background: var(--bg-hover, rgba(0, 122, 255, 0.08));
}

.sub-agent-rail__main-icon {
  flex-shrink: 0;
  display: inline-block;
  vertical-align: middle;
}

.sub-agent-rail__main-label {
  white-space: nowrap;
}

/* ── Spacer (vertical divider) between Main and the chip group ─────────── */
.sub-agent-rail__spacer {
  flex: 0 0 1px;
  align-self: center;
  width: 1px;
  height: 16px;
  background: var(--border);
  margin: 0 4px;
}

/* ── Chip ──────────────────────────────────────────────────────────────── */
.sub-agent-rail__chip {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  /* `flex: 0 0 auto` so chips keep their content width and never shrink to
   * pack in — matches ChatTabStrip__tab. Shrinking would defeat the measured
   * overflow probe. */
  flex: 0 0 auto;
  min-width: 10ch;
  max-width: clamp(12ch, 34ch, 42ch);
  height: 24px;
  padding: 0 6px 0 8px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 4px;
  cursor: pointer;
  color: var(--text-secondary);
  font: inherit;
  font-size: var(--text-sm);
  line-height: 1;
  user-select: none;
  transition: background-color 80ms ease, color 80ms ease, opacity 80ms ease;
}

.sub-agent-rail__chip:hover {
  background: var(--bg-hover, rgba(0, 0, 0, 0.04));
  color: var(--text-primary);
}

.sub-agent-rail__chip:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.sub-agent-rail__chip--active {
  background: var(--bg-hover, rgba(0, 122, 255, 0.08));
  color: var(--text-primary);
}

.sub-agent-rail__chip--active .sub-agent-rail__chip-title {
  font-weight: 600;
}

/* Closed chip (backend session exists but tab isn't loaded in memory). The
 * chip is dimmer + italic; the status dot goes grey (regardless of the
 * persisted status) to signal "memory-unloaded, click to hydrate". */
.sub-agent-rail__chip--offline {
  opacity: 0.6;
}

.sub-agent-rail__chip--offline .sub-agent-rail__chip-title {
  font-style: italic;
}

.sub-agent-rail__chip--offline:hover {
  opacity: 0.85;
}

/* Status dot — 8×8 round indicator, same 4-state palette as
 * ChatTabStrip.__dot. */
.sub-agent-rail__chip-dot {
  flex-shrink: 0;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
}

.sub-agent-rail__chip-dot--idle,
.sub-agent-rail__chip-dot--done {
  background: var(--text-muted);
}

.sub-agent-rail__chip-dot--running {
  background: var(--success, #16a34a);
}

.sub-agent-rail__chip-dot--aborting {
  background: var(--warning, #f59e0b);
}

.sub-agent-rail__chip-dot--error {
  background: var(--error, #dc2626);
}

/* Closed chip's dot always shows grey (open-tab absence is the truth; live
 * status is not observable without an open live stream). */
.sub-agent-rail__chip-dot--offline {
  background: var(--text-muted) !important;
  opacity: 0.6;
}

.sub-agent-rail__chip-title {
  flex: 1 1 auto;
  min-width: 0;
  text-overflow: ellipsis;
  overflow: hidden;
  white-space: nowrap;
  text-align: left;
}

/* Active-chip underline — implemented as an absolutely-positioned child so
 * the 100ms slide animation uses `transform` (compositor-only, no reflow). */
.sub-agent-rail__chip-underline {
  position: absolute;
  left: 8px;
  right: 8px;
  bottom: -1px;
  height: 2px;
  background: var(--accent);
  border-radius: 1px 1px 0 0;
  opacity: 0;
  transform: scaleX(0.6);
  transform-origin: center;
  transition:
    opacity 100ms ease,
    transform 100ms ease;
  pointer-events: none;
}

.sub-agent-rail__chip--active .sub-agent-rail__chip-underline {
  opacity: 1;
  transform: scaleX(1);
}

/* Per-chip close affordance (open chips only). Stays invisible until the
 * chip is hovered/focused/active. Uses <span role="button"> because nested
 * <button> inside a <button> is invalid HTML. */
.sub-agent-rail__chip-close {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
  transition:
    opacity 0.12s ease,
    background-color 0.12s ease,
    color 0.12s ease;
}

.sub-agent-rail__chip:hover .sub-agent-rail__chip-close,
.sub-agent-rail__chip:focus-within .sub-agent-rail__chip-close,
.sub-agent-rail__chip--active .sub-agent-rail__chip-close {
  opacity: 1;
}

.sub-agent-rail__chip-close:hover {
  background: var(--error, #dc2626);
  color: #fff;
}

.sub-agent-rail__chip-close:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
  opacity: 1;
}

/* ── Overflow trigger + popover ─────────────────────────────────────────── */
.sub-agent-rail__overflow {
  position: relative;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  padding: 0 6px;
}

.sub-agent-rail__overflow-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 22px;
  padding: 0 8px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 4px;
  cursor: pointer;
  color: var(--text-secondary);
  font: inherit;
  font-size: var(--text-sm);
  line-height: 1;
  white-space: nowrap;
  transition: background-color 80ms ease, color 80ms ease;
}

.sub-agent-rail__overflow-trigger:hover,
.sub-agent-rail__overflow-trigger--open {
  color: var(--text-primary);
  border-color: var(--accent);
}

.sub-agent-rail__overflow-trigger:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

/* Popover panel row/header styles — applied to slot content of
 * <ChatOverflowPopover>. Slot content is compiled by THIS component so
 * the scoped hash matches without `:deep()`. */
.sub-agent-rail__popover-header {
  padding: 6px 12px 4px;
  font-size: var(--text-xs);
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  border-bottom: 1px solid var(--border);
  margin-bottom: 4px;
}

.sub-agent-rail__popover-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  background: transparent;
  border: none;
  color: var(--text-primary);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  text-align: left;
}

.sub-agent-rail__popover-item:hover {
  background: var(--bg-hover, rgba(0, 0, 0, 0.06));
}

.sub-agent-rail__popover-item--active {
  background: var(--bg-hover, rgba(0, 122, 255, 0.08));
  font-weight: 600;
}

.sub-agent-rail__popover-item--offline {
  opacity: 0.6;
}

.sub-agent-rail__popover-item--offline .sub-agent-rail__popover-item-title {
  font-style: italic;
}

.sub-agent-rail__popover-item-title {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sub-agent-rail__popover-item-status {
  flex-shrink: 0;
  font-size: var(--text-xs);
  color: var(--text-muted);
}
</style>
