// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useModeIntroCardVisibility — 3-tier visibility gate for `ModeIntroCard`.
 *
 * Fix background: switching into App Builder / GoMaster / Model Builder mode
 * on a tab that already has messages (`activeMessages.length > 0`) hid the
 * empty-state welcome forever, so users lost the discoverability of the
 * mode's key resources + actions (Plan §7, decision 5 — C+D combo). This
 * composable owns the SINGLE truth for whether a mode-intro card should
 * render right now, keyed by mode.
 *
 * Three tiers (checked top-down, first match wins):
 *   1. Permanent  — `localStorage["modeIntro.hidden.<mode>"] === "1"`.
 *      Set by the user ticking "don't show again" then hitting × on the
 *      card. Can only be cleared from Settings → "Mode Intro Hints".
 *   2. This session — `sessionStorage["modeIntro.hidden.<mode>"] === "1"`.
 *      Set by hitting × WITHOUT the "don't show again" tick. Cleared when
 *      the browser tab closes.
 *   3. Otherwise  — the card renders.
 *
 * Storage is directly wired to `window.localStorage` / `window.sessionStorage`
 * (rather than a Pinia store) because:
 *   - the state is bi-modal per-tier (session vs local), which is exactly
 *     what these APIs model natively;
 *   - no reactivity is needed across other components — the settings toggle
 *     re-reads on mount and the card re-reads on `resetToken()` bump.
 *
 * Reactivity model: a shared `ref` bump token is exposed as `resetToken`; the
 * ModeIntroCard's `shouldShow` derives from `computed(() => resetToken.value,
 * ...)` so a settings toggle that calls `clearHidden(mode)` immediately
 * re-shows the card without a page reload.
 */
import { computed, ref, type ComputedRef } from "vue";

export type IntroMode = "app-builder" | "gomaster" | "model-build" | "model-hub" | "pro" | "code";

/** Storage key for the given mode (single source of truth). */
function storageKey(mode: IntroMode): string {
  return `modeIntro.hidden.${mode}`;
}

// Module-level bump token: any mutation (setHidden / clearHidden) bumps this
// so every consumer's `shouldShow` computed re-evaluates. Kept module-level
// (not per-hook) so a Settings toggle can flip visibility for a live card.
const _resetToken = ref(0);

/** Read localStorage safely (SSR / privacy-mode fallback = not hidden). */
function readLocal(mode: IntroMode): boolean {
  try {
    return window.localStorage.getItem(storageKey(mode)) === "1";
  } catch {
    return false;
  }
}

/** Read sessionStorage safely. */
function readSession(mode: IntroMode): boolean {
  try {
    return window.sessionStorage.getItem(storageKey(mode)) === "1";
  } catch {
    return false;
  }
}

/**
 * True when the intro card for `mode` should be RENDERED. Cheap enough to
 * call from a `v-if` guard.
 */
export function isIntroHidden(mode: IntroMode): boolean {
  return readLocal(mode) || readSession(mode);
}

/**
 * Permanently hide the intro card for `mode` (localStorage tier). Called
 * when the user hits × with the "don't show again" checkbox ticked.
 */
export function hidePermanently(mode: IntroMode): void {
  try {
    window.localStorage.setItem(storageKey(mode), "1");
  } catch {
    // Storage unavailable — silently degrade (card will re-appear on reload).
  }
  _resetToken.value += 1;
}

/**
 * Hide the intro card for this session only (sessionStorage tier). Called
 * when the user hits × WITHOUT the "don't show again" tick.
 */
export function hideForSession(mode: IntroMode): void {
  try {
    window.sessionStorage.setItem(storageKey(mode), "1");
  } catch {
    // Storage unavailable — silently degrade.
  }
  _resetToken.value += 1;
}

/**
 * Clear ALL "hidden" markers for `mode` (both local + session). Called by
 * the Settings toggle to let a user restore a permanently-hidden intro.
 */
export function clearHidden(mode: IntroMode): void {
  try {
    window.localStorage.removeItem(storageKey(mode));
  } catch {
    // ignore
  }
  try {
    window.sessionStorage.removeItem(storageKey(mode));
  } catch {
    // ignore
  }
  _resetToken.value += 1;
}

/**
 * Reactive "should this mode's intro card show?" computed. The bump token
 * makes it reactive to `setHidden` / `clearHidden` mutations even though
 * `localStorage` / `sessionStorage` are not natively reactive.
 */
export function useModeIntroCardVisibility(mode: IntroMode): {
  shouldShow: ComputedRef<boolean>;
  isPermanentlyHidden: ComputedRef<boolean>;
  hidePermanently: () => void;
  hideForSession: () => void;
  clearHidden: () => void;
} {
  const shouldShow = computed<boolean>(() => {
    // Depend on the reset token so a mutation forces re-evaluation.
    void _resetToken.value;
    return !isIntroHidden(mode);
  });
  const isPermanentlyHidden = computed<boolean>(() => {
    void _resetToken.value;
    return readLocal(mode);
  });
  return {
    shouldShow,
    isPermanentlyHidden,
    hidePermanently: () => hidePermanently(mode),
    hideForSession: () => hideForSession(mode),
    clearHidden: () => clearHidden(mode),
  };
}
