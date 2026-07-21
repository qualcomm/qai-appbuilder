<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModeIntroCard — top-right overlay onboarding helper for App Builder /
 * GoMaster / Model Builder / Pro / Code modes.
 *
 * UX contract (2026-07-17 refresh):
 *   1. **Empty conversation** — the mode's dedicated empty-state screen
 *      (AppBuilderEmptyState / GomasterEmptyState / ProEmptyState /
 *      CodeEmptyState / etc.) already provides the full 3-step onboarding
 *      as a landing surface. The parent (ChatView) suppresses this card
 *      entirely in that case. This component is designed for the
 *      "conversation has messages" state.
 *   2. **Conversation has messages** — a subtle ⓘ button in the top-right
 *      of the chat viewport. Clicking it pops out a 3-step reference card
 *      immediately below the button. The card is dismissable (× or clicking
 *      outside), and an optional "don't show again" checkbox permanently
 *      hides the button + card for the mode (localStorage). Users who
 *      dismissed permanently can restore from Settings → Mode Intro Hints.
 *
 * This replaces the earlier "always-on inline strip above the composer"
 * design, which cost a permanent row of vertical space and read as clutter
 * once users had learned the mode. See git history for the transition
 * rationale.
 *
 * Visibility model (3-tier, unchanged): permanent → session → visible.
 * Handled by `useModeIntroCardVisibility` so a Settings-page toggle can
 * flip live components without a page reload.
 */
import { computed, onMounted, onBeforeUnmount, ref } from "vue";
import { useI18n } from "vue-i18n";

import {
  useModeIntroCardVisibility,
  type IntroMode,
} from "@/composables/useModeIntroCardVisibility";

interface Props {
  /** The mode whose intro is shown; determines i18n scope + chip actions. */
  mode: IntroMode;
}
const props = defineProps<Props>();

const emit = defineEmits<{
  /** A chip whose action is to FILL the composer with a starter prompt. */
  "fill-prompt": [prompt: string];
  /**
   * A chip whose action is a semantic UI navigation — the parent decides
   * what to do (`open-my-apps` / `open-promote` / `open-optimize` / …).
   * Kept as a stable string id so new modes can add new chip actions without
   * changing the emit shape.
   */
  action: [id: string];
}>();

const { t } = useI18n();
const {
  shouldShow,
  hidePermanently,
  hideForSession,
} = useModeIntroCardVisibility(props.mode);

// Whether the pop-out card is currently open. Starts closed on every mount
// — the ⓘ button is the discoverable affordance; the card only appears
// when the user actively clicks the button. This is deliberately per-mount
// state (no localStorage) so switching modes / reloading gets a clean
// closed state.
const open = ref<boolean>(false);
const dontShowAgain = ref<boolean>(false);

// One-shot "attention" pulse on the trigger so users notice a brand-new
// affordance in the top-right corner (feedback: the previous plain grey
// icon was too easy to overlook). Runs for ~2.4 s on mount, then stops
// permanently — avoids the trap where a persistently animating button
// keeps stealing attention from actual chat content.
//
// Restarts whenever the ACTIVE MODE changes (props.mode is reactive
// through the parent's v-if key on `introMode`), which is exactly when
// the guidance is most likely to be relevant (mode switch = user is
// probably new to this workflow).
const showAttention = ref<boolean>(true);
let _attentionTimer: ReturnType<typeof setTimeout> | null = null;
function startAttentionPulse(): void {
  showAttention.value = true;
  if (_attentionTimer !== null) clearTimeout(_attentionTimer);
  _attentionTimer = setTimeout(() => {
    showAttention.value = false;
    _attentionTimer = null;
  }, 2400);
}

// ── Content driven by `mode` ────────────────────────────────────────────────

interface Chip {
  /** Stable id emitted via `action` (or the label used as fill-prompt text). */
  id: string;
  /** Localized label. */
  label: string;
  /** Chip semantics: fill the composer with the label, or emit a nav action. */
  kind: "fill" | "action";
}

const steps = computed<string[]>(() => {
  switch (props.mode) {
    case "app-builder":
      return [
        t("modeIntro.appBuilder.step1"),
        t("modeIntro.appBuilder.step2"),
        t("modeIntro.appBuilder.step3"),
      ];
    case "gomaster":
      return [
        t("modeIntro.gomaster.step1"),
        t("modeIntro.gomaster.step2"),
        t("modeIntro.gomaster.step3"),
      ];
    case "model-build":
      return [
        t("modeIntro.modelBuilder.step1"),
        t("modeIntro.modelBuilder.step2"),
        t("modeIntro.modelBuilder.step3"),
      ];
    case "model-hub":
      return [
        t("modeIntro.modelHub.step1"),
        t("modeIntro.modelHub.step2"),
        t("modeIntro.modelHub.step3"),
      ];
    case "pro":
      return [
        t("modeIntro.pro.step1"),
        t("modeIntro.pro.step2"),
        t("modeIntro.pro.step3"),
      ];
    case "code":
      return [
        t("modeIntro.code.step1"),
        t("modeIntro.code.step2"),
        t("modeIntro.code.step3"),
      ];
  }
});

const chips = computed<Chip[]>(() => {
  switch (props.mode) {
    case "app-builder":
      return [
        { id: "open-my-apps", label: t("modeIntro.appBuilder.chipMyApps"), kind: "action" },
        { id: "open-promote", label: t("modeIntro.appBuilder.chipPromote"), kind: "action" },
      ];
    case "gomaster":
      return [
        { id: "open-optimize", label: t("modeIntro.gomaster.chipOptimize"), kind: "action" },
      ];
    case "model-build":
      return [
        { id: "open-promote", label: t("modeIntro.modelBuilder.chipPromote"), kind: "action" },
      ];
    case "model-hub":
      return [
        { id: "open-promote", label: t("modeIntro.modelHub.chipPromote"), kind: "action" },
      ];
    case "pro":
      return [
        { id: "open-pro-settings", label: t("modeIntro.pro.chipSettings"), kind: "action" },
        { id: "open-pro-connect", label: t("modeIntro.pro.chipConnect"), kind: "action" },
      ];
    case "code":
      return [
        { id: "open-code-persona", label: t("modeIntro.code.chipPersona"), kind: "action" },
        { id: "open-code-context", label: t("modeIntro.code.chipContext"), kind: "action" },
      ];
  }
});

const titleKey = computed<string>(() => {
  switch (props.mode) {
    case "app-builder":
      return "modeIntro.appBuilder.title";
    case "gomaster":
      return "modeIntro.gomaster.title";
    case "model-build":
      return "modeIntro.modelBuilder.title";
    case "model-hub":
      return "modeIntro.modelHub.title";
    case "pro":
      return "modeIntro.pro.title";
    case "code":
      return "modeIntro.code.title";
  }
});

const subtitleKey = computed<string>(() => {
  switch (props.mode) {
    case "app-builder":
      return "modeIntro.appBuilder.subtitle";
    case "gomaster":
      return "modeIntro.gomaster.subtitle";
    case "model-build":
      return "modeIntro.modelBuilder.subtitle";
    case "model-hub":
      return "modeIntro.modelHub.subtitle";
    case "pro":
      return "modeIntro.pro.subtitle";
    case "code":
      return "modeIntro.code.subtitle";
  }
});

// ── Handlers ────────────────────────────────────────────────────────────────

function toggleOpen(): void {
  open.value = !open.value;
  // First user interaction with the trigger → they've clearly noticed it;
  // no reason to keep pulsing. This also fires when they close the
  // pop-out (open→false), which is intentional: whatever the reason for
  // the click, the attention job is done.
  if (showAttention.value) {
    showAttention.value = false;
    if (_attentionTimer !== null) {
      clearTimeout(_attentionTimer);
      _attentionTimer = null;
    }
  }
}

function closePopover(): void {
  open.value = false;
}

/**
 * The × on the card. Two branches:
 *   - dontShowAgain checked → permanent (localStorage); the ⓘ button also
 *     goes away because `shouldShow` flips false.
 *   - unchecked → just close the pop-out; nothing persists. The ⓘ stays
 *     visible so the user can re-open on demand.
 */
function onClose(): void {
  if (dontShowAgain.value) {
    hidePermanently();
    // shouldShow now false → whole overlay unmounts.
  } else {
    // Not a dismissal — just collapse the pop-out. Do NOT flip
    // `hideForSession` here; the button should stay reachable for the
    // rest of the session.
    open.value = false;
  }
}

function onChipClick(chip: Chip): void {
  if (chip.kind === "fill") {
    emit("fill-prompt", chip.label);
  } else {
    emit("action", chip.id);
  }
  // Chip click implies the user got what they came for — collapse the
  // pop-out so it stops covering the chat.
  open.value = false;
}

// ── Outside-click + Esc close ────────────────────────────────────────────────
// Using a single click listener on document with a ref-based hit-test is
// simpler than clickOutside directives and does not need the overlay to
// stop event propagation on every interior element.
const rootRef = ref<HTMLElement | null>(null);

function onDocClick(evt: MouseEvent): void {
  if (!open.value) return;
  const el = rootRef.value;
  if (el === null) return;
  const target = evt.target as Node | null;
  if (target !== null && el.contains(target)) return;
  open.value = false;
}

function onDocKey(evt: KeyboardEvent): void {
  if (evt.key === "Escape" && open.value) {
    open.value = false;
  }
}

onMounted(() => {
  document.addEventListener("click", onDocClick, true);
  document.addEventListener("keydown", onDocKey);
  // Kick off the attention pulse on first paint so users notice the
  // top-right affordance. Runs once per mount; the parent uses `:mode`
  // as the vnode key so switching modes remounts the component and
  // pulses again for the new mode's guidance.
  startAttentionPulse();
});
onBeforeUnmount(() => {
  document.removeEventListener("click", onDocClick, true);
  document.removeEventListener("keydown", onDocKey);
  // Clear any in-flight attention timer to avoid a leaked setTimeout that
  // touches a stale ref after unmount.
  if (_attentionTimer !== null) {
    clearTimeout(_attentionTimer);
    _attentionTimer = null;
  }
});

// Deliberately marked as used so the intentionally-unused `hideForSession`
// export from the composable does not trigger `noUnusedLocals` when the
// close handler above chooses not to invoke it. Kept as an explicit
// discard so a future revision can add the per-session-dismiss path back
// without re-wiring the destructure.
void hideForSession;
</script>

<template>
  <div
    v-if="shouldShow"
    ref="rootRef"
    class="mode-intro-overlay"
    :data-mode="mode"
    :data-testid="`mode-intro-overlay-${mode}`"
  >
    <!-- Trigger button — always visible when shouldShow=true. Uses the
         brand accent color + a one-shot attention pulse on first paint so
         it is discoverable at a glance (feedback: a subdued grey circle
         was invisible against the top toolbar). The pulse stops after a
         short window so it does not keep grabbing attention. -->
    <button
      type="button"
      class="mode-intro-overlay__trigger"
      :class="{
        'mode-intro-overlay__trigger--active': open,
        'mode-intro-overlay__trigger--attention': showAttention,
      }"
      :aria-expanded="open"
      :aria-label="t(titleKey)"
      :title="t(titleKey)"
      :data-testid="`mode-intro-trigger-${mode}`"
      @click="toggleOpen"
    >
      <!-- ⓘ (U+24D8) rendered as text so it inherits color / font-size
           cleanly across themes without needing an inline SVG. -->
      <span class="mode-intro-overlay__trigger-icon" aria-hidden="true">ⓘ</span>
      <!-- Text label makes the button self-explanatory ("引导" / "Guide").
           Hidden on very narrow viewports (< 480 px) via a media query in
           <style> so the button collapses to an icon-only bubble on mobile
           but stays labelled everywhere else. -->
      <span class="mode-intro-overlay__trigger-label">
        {{ t("modeIntro.triggerLabel") }}
      </span>
    </button>

    <!-- Pop-out card. Absolutely positioned below the trigger so it does
         NOT push chat content around when opening / closing. Own scroll
         if content overflows on very small viewports. -->
    <transition name="mode-intro-pop">
      <div
        v-if="open"
        class="mode-intro-overlay__pop"
        role="dialog"
        :aria-label="t(titleKey)"
        :data-testid="`mode-intro-pop-${mode}`"
      >
        <header class="mode-intro-overlay__head">
          <h3 class="mode-intro-overlay__title">{{ t(titleKey) }}</h3>
          <button
            type="button"
            class="mode-intro-overlay__close"
            :aria-label="t('common.close')"
            :title="t('common.close')"
            :data-testid="`mode-intro-close-${mode}`"
            @click="onClose"
          >×</button>
        </header>
        <p class="mode-intro-overlay__subtitle">{{ t(subtitleKey) }}</p>
        <ol class="mode-intro-overlay__steps">
          <li
            v-for="(step, i) in steps"
            :key="i"
            class="mode-intro-overlay__step"
          >
            <span class="mode-intro-overlay__step-num">{{ i + 1 }}</span>
            <span class="mode-intro-overlay__step-text">{{ step }}</span>
          </li>
        </ol>
        <div class="mode-intro-overlay__chips">
          <button
            v-for="chip in chips"
            :key="chip.id"
            type="button"
            class="mode-intro-overlay__chip"
            :data-testid="`mode-intro-chip-${chip.id}`"
            @click="onChipClick(chip)"
          >{{ chip.label }}</button>
        </div>
        <label class="mode-intro-overlay__dont-show">
          <input
            v-model="dontShowAgain"
            type="checkbox"
            :data-testid="`mode-intro-dont-show-${mode}`"
          />
          <span>{{ t("modeIntro.dontShowAgain") }}</span>
        </label>
      </div>
    </transition>
  </div>
</template>

<style scoped>
/*
 * The overlay is anchored to the top-right of its offset parent — the
 * ChatView already has a positioned wrapper suitable for this. The trigger
 * sits at the corner; the pop-out drops directly under it and does not
 * flow content around (position: absolute). See the parent for the
 * containing block.
 */
.mode-intro-overlay {
  /* An inline placeholder so the trigger renders inline in the
     right-aligned toolbar row where ChatView places this component. */
  position: relative;
  display: inline-flex;
  align-items: center;
  z-index: 20;
}

/* ── Trigger button (ⓘ + label pill) ───────────────────────────────────── */
/*
 * Redesigned as a pill (icon + text label) with the brand accent color so
 * it is clearly discoverable against the dark chat surface. The previous
 * design was a subdued grey circle that users tuned out entirely
 * (feedback: "the icon is not eye-catching, users don't notice it").
 *
 * Rules of thumb:
 *   - Solid brand-accent background reads as "clickable primary
 *     affordance" (vs the toolbar buttons which are outline / ghost);
 *   - The text label ("引导" / "Guide") makes the purpose obvious at a
 *     glance so users don't have to hover for a tooltip. Collapses to
 *     icon-only on very narrow viewports (< 480px).
 *   - A one-shot pulse animation runs on mount (`--attention` modifier)
 *     to draw the eye during the first ~2.4 s, then stops so it never
 *     becomes a permanent visual noise source.
 */
.mode-intro-overlay__trigger {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px 0 10px;
  border-radius: 999px;
  border: 1px solid var(--accent, #6d5efc);
  background: var(--accent, #6d5efc);
  color: #ffffff;
  font-size: 0.82rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
  box-shadow: 0 2px 8px rgba(109, 94, 252, 0.35);
}
.mode-intro-overlay__trigger:hover,
.mode-intro-overlay__trigger:focus-visible {
  background: color-mix(in oklab, var(--accent, #6d5efc) 88%, white 12%);
  border-color: color-mix(in oklab, var(--accent, #6d5efc) 88%, white 12%);
  outline: none;
  box-shadow: 0 3px 12px rgba(109, 94, 252, 0.5);
}
.mode-intro-overlay__trigger--active {
  background: color-mix(in oklab, var(--accent, #6d5efc) 80%, black 20%);
  border-color: color-mix(in oklab, var(--accent, #6d5efc) 80%, black 20%);
}
.mode-intro-overlay__trigger-icon {
  font-size: 1rem;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.mode-intro-overlay__trigger-label {
  line-height: 1;
  letter-spacing: 0.02em;
}
/* One-shot attention pulse — mirrors the pattern used in
 * AppBuilderEmptyState (`ab-chip-attention`). The parent adds
 * `--attention` on mount and removes it after ~2.4s so this only runs
 * once per mode entry. */
.mode-intro-overlay__trigger--attention {
  animation: mode-intro-trigger-attention 2.4s ease-out 1;
}
@keyframes mode-intro-trigger-attention {
  0%, 100% {
    box-shadow: 0 2px 8px rgba(109, 94, 252, 0.35);
  }
  25% {
    box-shadow:
      0 2px 8px rgba(109, 94, 252, 0.35),
      0 0 0 6px rgba(109, 94, 252, 0.35);
  }
  50% {
    box-shadow:
      0 2px 8px rgba(109, 94, 252, 0.35),
      0 0 0 12px rgba(109, 94, 252, 0);
  }
  75% {
    box-shadow:
      0 2px 8px rgba(109, 94, 252, 0.35),
      0 0 0 6px rgba(109, 94, 252, 0.28);
  }
}
/* Accessibility: users who prefer reduced motion get neither the pulse
 * nor any of the entry animation on the pop-out. The button remains
 * highly visible thanks to its solid accent colour + label. */
@media (prefers-reduced-motion: reduce) {
  .mode-intro-overlay__trigger--attention {
    animation: none;
  }
}
/* Very narrow viewports — collapse the label so the pill becomes an
 * icon-only circle to conserve horizontal space. */
@media (max-width: 480px) {
  .mode-intro-overlay__trigger {
    padding: 0;
    width: 32px;
    justify-content: center;
  }
  .mode-intro-overlay__trigger-label {
    display: none;
  }
}

/* ── Pop-out card ────────────────────────────────────────────────────────── */

.mode-intro-overlay__pop {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: min(380px, calc(100vw - 48px));
  max-height: calc(100vh - 160px);
  overflow: auto;
  background: var(--bg-secondary, #1c1c22);
  border: 1px solid var(--border, #2b2b33);
  border-radius: var(--radius-md, 10px);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  color: var(--text-primary, #e6e6e6);
  padding: 14px 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  z-index: 30;
}

/* Enter/leave: quick fade + slight drop so the pop-out feels attached to
   the trigger. Reduced-motion users get an instant appearance. */
.mode-intro-pop-enter-from,
.mode-intro-pop-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
.mode-intro-pop-enter-active,
.mode-intro-pop-leave-active {
  transition: opacity 0.14s ease, transform 0.14s ease;
}
@media (prefers-reduced-motion: reduce) {
  .mode-intro-pop-enter-active,
  .mode-intro-pop-leave-active {
    transition: none;
  }
}

.mode-intro-overlay__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.mode-intro-overlay__title {
  flex: 1 1 auto;
  margin: 0;
  font-size: 0.95rem;
  font-weight: 600;
  line-height: 1.3;
  color: var(--text-primary, #f5f5f5);
}

.mode-intro-overlay__close {
  flex: 0 0 auto;
  width: 24px;
  height: 24px;
  border-radius: var(--radius-sm, 6px);
  background: transparent;
  border: none;
  color: var(--text-secondary, #a0a0a8);
  font-size: 1.1rem;
  line-height: 1;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background 0.12s ease, color 0.12s ease;
}
.mode-intro-overlay__close:hover,
.mode-intro-overlay__close:focus-visible {
  background: var(--bg-hover, rgba(255, 255, 255, 0.06));
  color: var(--text-primary, #e6e6e6);
  outline: none;
}

.mode-intro-overlay__subtitle {
  margin: 0;
  font-size: 0.85rem;
  line-height: 1.5;
  color: var(--text-secondary, #a0a0a8);
}

.mode-intro-overlay__steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.mode-intro-overlay__step {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 0.85rem;
  line-height: 1.45;
  color: var(--text-primary, #e6e6e6);
}

.mode-intro-overlay__step-num {
  flex: 0 0 auto;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--bg-tertiary, #2a2a33);
  color: var(--text-secondary, #a0a0a8);
  font-size: 0.72rem;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-top: 1px;
}

.mode-intro-overlay__step-text {
  flex: 1 1 auto;
}

.mode-intro-overlay__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.mode-intro-overlay__chip {
  padding: 5px 12px;
  background: transparent;
  border: 1px solid var(--border, #34343f);
  border-radius: 999px;
  color: var(--text-primary, #d8d8e0);
  font-size: 0.8rem;
  cursor: pointer;
  transition: background 0.12s ease, border-color 0.12s ease;
}
.mode-intro-overlay__chip:hover,
.mode-intro-overlay__chip:focus-visible {
  background: var(--bg-hover, rgba(255, 255, 255, 0.04));
  border-color: var(--border-light, #4a4a58);
  outline: none;
}

.mode-intro-overlay__dont-show {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 0.78rem;
  color: var(--text-secondary, #8a8a92);
  cursor: pointer;
  user-select: none;
  align-self: flex-start;
}

.mode-intro-overlay__dont-show input[type="checkbox"] {
  accent-color: var(--accent, #6d5efc);
  cursor: pointer;
}
</style>
