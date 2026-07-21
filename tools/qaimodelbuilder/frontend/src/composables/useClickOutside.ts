// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useClickOutside / useEscClose — generic dismiss primitives for popovers,
 * dropdowns, and modals.
 *
 * `useClickOutside(targetRef, onOutside, opts?)` registers a `click`
 * listener (default `capture=true`) on `document` and invokes `onOutside`
 * whenever the click lands outside `targetRef.value` (or any of its
 * descendants). Mirrors the V1 sidebar pattern (`AppSidebar.vue`'s former
 * `onDocumentClick`) without the host having to manage the listener
 * lifecycle.
 *
 * `useEscClose(onEsc, when?)` registers a `keydown` listener on
 * `document` and invokes `onEsc` when the user presses `Escape`. The
 * optional `when` predicate short-circuits the dismiss when the parent
 * widget is closed (avoiding wasted work on every keystroke globally).
 *
 * Both helpers self-register on `onMounted` and self-unregister on
 * `onBeforeUnmount` so callers stay honest with the Vue lifecycle.
 *
 * Capture-phase default rationale: the V1 sidebar registered with
 * `capture=true` so an inner button's `@click.stop` does NOT swallow
 * the outside-click signal at document-bubble phase. We keep the same
 * default to preserve V1 behaviour.
 */
import { onBeforeUnmount, onMounted, type Ref } from "vue";

export interface UseClickOutsideOptions {
  /**
   * `addEventListener` capture flag. Defaults to `true` to match the V1
   * sidebar pattern: capture phase fires before any child `@click.stop`
   * can swallow the event, so popovers reliably close on outside clicks
   * regardless of inner-button propagation choices.
   */
  capture?: boolean;
  /**
   * Optional predicate gating the dismiss. When provided and falsy, the
   * outside-click signal is ignored. Lets a closed popover skip the
   * `contains()` walk entirely.
   */
  when?: () => boolean;
  /**
   * Pointer event to listen on. Defaults to `"click"` (works for the
   * majority of dropdown / popover dismiss scenarios). Set to
   * `"mousedown"` to match V1 widgets that close on press-down rather
   * than click — this catches outside-press during a long drag and
   * matches the V1 app-builder picker / variant-switcher behaviour
   * exactly.
   */
  event?: "click" | "mousedown";
}

/**
 * Invoke `onOutside(event)` when the user clicks anywhere outside the
 * element pointed at by `targetRef`. Self-registers / unregisters with
 * the Vue lifecycle.
 */
export function useClickOutside(
  targetRef: Ref<HTMLElement | null>,
  onOutside: (ev: MouseEvent) => void,
  opts: UseClickOutsideOptions = {},
): void {
  const capture = opts.capture ?? true;
  const eventType = opts.event ?? "click";
  const handler = (ev: MouseEvent): void => {
    if (opts.when && !opts.when()) return;
    const el = targetRef.value;
    if (!el) return;
    const target = ev.target;
    if (target instanceof Node && !el.contains(target)) {
      onOutside(ev);
    }
  };
  onMounted(() => {
    document.addEventListener(eventType, handler, capture);
  });
  onBeforeUnmount(() => {
    document.removeEventListener(eventType, handler, capture);
  });
}

/**
 * Invoke `onEsc(event)` when the user presses `Escape`. Optional `when`
 * predicate gates the dismiss (typically `() => popoverOpen.value`).
 * Self-registers / unregisters with the Vue lifecycle.
 *
 * The handler receives the `KeyboardEvent` so callers can call
 * `event.preventDefault()` to stop a parent dialog (or the browser)
 * from also reacting to Escape.
 */
export function useEscClose(
  onEsc: (ev: KeyboardEvent) => void,
  when?: () => boolean,
): void {
  const handler = (ev: KeyboardEvent): void => {
    if (ev.key !== "Escape") return;
    if (when && !when()) return;
    onEsc(ev);
  };
  onMounted(() => {
    document.addEventListener("keydown", handler);
  });
  onBeforeUnmount(() => {
    document.removeEventListener("keydown", handler);
  });
}
