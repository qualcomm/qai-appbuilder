// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useFocusTrap — modal focus-trap composable.
 *
 * V1 truth source: `QAIModelBuilder_v1_pure/frontend/js/utils/focus-trap.js`
 * (`trapFocus(containerEl, opts)`). V1 behaviour distilled:
 *   1. Tab / Shift+Tab cycle focus within the container's focusable
 *      descendants (no escape to background while open).
 *   2. On activate, move initial focus into the container (first focusable
 *      or a caller-supplied element).
 *   3. On deactivate, restore focus to the element that was focused before
 *      the modal opened (if still in the DOM).
 *
 * V2 first surfaced this behaviour ad-hoc in
 * `components/layout/ConfirmDialog.vue` (lines 20-80). Six-plus other modals
 * had no Tab trap at all (only Esc / Enter), so keyboard users could Tab out
 * of an open dialog into the page underneath. This composable extracts the
 * one V1-aligned implementation so every modal gets the same behaviour with
 * a single line of setup code (cohesion / DRY: repeated trap logic across
 * RenameDialog / ConfirmDialog / SecurityDialog / ChannelInfoDialog /
 * PromptSnapshotPanel collapses into one source of truth).
 *
 * Usage:
 *   const containerRef = ref<HTMLElement | null>(null);
 *   const open = ref(false);
 *   useFocusTrap(containerRef, { active: open, onEscape: () => close() });
 *
 *   <template>
 *     <div v-if="open" ref="containerRef" role="dialog" aria-modal="true">
 *       ...
 *     </div>
 *   </template>
 *
 * The composable owns lifecycle (watch + onBeforeUnmount cleanup); callers
 * just bind a ref + an `active` flag.
 */
import { onBeforeUnmount, watch, nextTick, type Ref } from "vue";

export interface UseFocusTrapOptions {
  /**
   * Reactive open/closed flag. When it flips true → activate the trap,
   * when it flips false → release. If omitted, the caller must call
   * `activate()` / `deactivate()` manually.
   */
  active?: Ref<boolean>;
  /**
   * Optional Escape-key handler. When the trap is active and the user
   * presses Escape, this callback fires; the container is responsible for
   * deciding whether to close. If omitted, Escape does nothing (callers
   * that already bind their own keydown listener will handle it).
   */
  onEscape?: () => void;
  /**
   * Optional CSS selector for the element that should receive the initial
   * focus on activate. Falls back to the last focusable element (matches
   * V2's pre-existing ConfirmDialog behaviour, which focused the confirm
   * button by default — primary action gets focus).
   */
  initialFocusSelector?: string;
  /**
   * If true, initial focus goes to the FIRST focusable descendant (V1
   * `trapFocus` default). If false (default), goes to the LAST — matching
   * V2 ConfirmDialog's prior behaviour of focusing the primary "Confirm"
   * button. Most info-style dialogs without a primary action should pass
   * `focusFirst: true`.
   */
  focusFirst?: boolean;
}

export interface UseFocusTrapReturn {
  /** Manually activate the trap (when `active` is not bound). */
  activate: () => void;
  /** Manually release the trap and restore focus. */
  deactivate: () => void;
}

const FOCUSABLE_SELECTOR = [
  'a[href]',
  "button:not([disabled])",
  'input:not([disabled]):not([type="hidden"])',
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function getFocusable(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (el) => !el.hasAttribute("disabled"),
  );
}

export function useFocusTrap(
  containerRef: Ref<HTMLElement | null>,
  options: UseFocusTrapOptions = {},
): UseFocusTrapReturn {
  let lastFocused: HTMLElement | null = null;
  let attached = false;

  function onKeydown(e: KeyboardEvent): void {
    const root = containerRef.value;
    if (root === null) return;
    if (e.key === "Escape") {
      if (options.onEscape) {
        e.preventDefault();
        options.onEscape();
      }
      return;
    }
    if (e.key !== "Tab") return;

    const focusable = getFocusable(root);
    if (focusable.length === 0) {
      e.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (first === undefined || last === undefined) return;
    const active = document.activeElement as HTMLElement | null;
    if (e.shiftKey) {
      if (active === first || active === null || !root.contains(active)) {
        e.preventDefault();
        last.focus();
      }
    } else if (active === last || active === null || !root.contains(active)) {
      e.preventDefault();
      first.focus();
    }
  }

  function activate(): void {
    if (attached) return;
    lastFocused = document.activeElement as HTMLElement | null;
    window.addEventListener("keydown", onKeydown);
    attached = true;
    // Move initial focus once the dialog is rendered.
    void nextTick(() => {
      const root = containerRef.value;
      if (root === null) return;
      let target: HTMLElement | null = null;
      if (options.initialFocusSelector) {
        target = root.querySelector<HTMLElement>(options.initialFocusSelector);
      }
      if (target === null) {
        const focusable = getFocusable(root);
        if (focusable.length > 0) {
          const candidate =
            options.focusFirst === true ? focusable[0] : focusable[focusable.length - 1];
          target = candidate ?? null;
        }
      }
      try {
        target?.focus({ preventScroll: true });
      } catch {
        /* noop */
      }
    });
  }

  function deactivate(): void {
    if (!attached) return;
    window.removeEventListener("keydown", onKeydown);
    attached = false;
    // Restore focus to the element that opened the dialog (V1 parity:
    // trapFocus's `release()` closure restores `previouslyFocused`).
    try {
      if (
        lastFocused !== null &&
        document.contains(lastFocused) &&
        typeof lastFocused.focus === "function"
      ) {
        lastFocused.focus({ preventScroll: true });
      }
    } catch {
      /* noop */
    }
    lastFocused = null;
  }

  if (options.active !== undefined) {
    const flag = options.active;
    watch(
      flag,
      (v) => {
        if (v) {
          activate();
        } else {
          deactivate();
        }
      },
      { immediate: true },
    );
  }

  onBeforeUnmount(() => {
    deactivate();
  });

  return { activate, deactivate };
}
