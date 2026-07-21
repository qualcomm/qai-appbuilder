// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useKeymap — global keyboard shortcut composable (refactor-plan §8.9).
 *
 * Replaces the legacy frontend/js/app.js hand-rolled `keydown` listener
 * (which dispatched against a global `currentView` and a hash router).
 * The new implementation:
 *
 *   - Binds a single listener on `document` per consumer instance.
 *   - Uses a declarative `KeymapBinding[]` so consumers (typically
 *     `App.vue` or a top-level layout component) can register tab /
 *     palette / theme shortcuts in one place.
 *   - Cleans up on unmount via `onScopeDispose`.
 *   - Skips events whose target is an editable element (input /
 *     textarea / contenteditable) unless the binding opts-in via
 *     `preventInEditable: false`.
 */
import { onScopeDispose } from "vue";

export interface KeymapBinding {
  /** A KeyboardEvent.code or KeyboardEvent.key value. Code is preferred. */
  readonly key: string;
  /** When true, requires the Ctrl (Win/Linux) or Cmd (macOS) modifier. */
  readonly ctrlOrMeta?: boolean;
  /** When true, requires the Shift modifier. */
  readonly shift?: boolean;
  /** When true, requires the Alt modifier. */
  readonly alt?: boolean;
  /** Handler. Receives the original event so it can call preventDefault. */
  readonly handler: (event: KeyboardEvent) => void;
  /** Default true — skip when event.target is an editable element. */
  readonly skipInEditable?: boolean;
}

export interface UseKeymapOptions {
  /** Override the target. Defaults to `document`. */
  readonly target?: Document | HTMLElement;
  /** When false, no listener is installed — useful for tests. */
  readonly enabled?: boolean;
}

const EDITABLE_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT"]);

function isEditableTarget(target: EventTarget | null): boolean {
  if (target === null) {
    return false;
  }
  if (target instanceof HTMLElement) {
    if (EDITABLE_TAGS.has(target.tagName)) {
      return true;
    }
    if (target.isContentEditable) {
      return true;
    }
  }
  return false;
}

function matches(event: KeyboardEvent, binding: KeymapBinding): boolean {
  if (event.key !== binding.key && event.code !== binding.key) {
    return false;
  }
  const wantCtrlMeta = binding.ctrlOrMeta === true;
  const hasCtrlMeta = event.ctrlKey === true || event.metaKey === true;
  if (wantCtrlMeta !== hasCtrlMeta) {
    return false;
  }
  if ((binding.shift === true) !== (event.shiftKey === true)) {
    return false;
  }
  if ((binding.alt === true) !== (event.altKey === true)) {
    return false;
  }
  return true;
}

export interface UseKeymap {
  install(): void;
  uninstall(): void;
  isInstalled(): boolean;
}

export function useKeymap(
  bindings: readonly KeymapBinding[],
  options: UseKeymapOptions = {},
): UseKeymap {
  const enabled = options.enabled ?? true;
  let installed = false;
  let detach: (() => void) | null = null;

  function onKeyDown(event: KeyboardEvent): void {
    for (const binding of bindings) {
      if (!matches(event, binding)) {
        continue;
      }
      const skipEditable = binding.skipInEditable ?? true;
      if (skipEditable && isEditableTarget(event.target)) {
        continue;
      }
      binding.handler(event);
      // First match wins to avoid double-fires on overlapping bindings.
      return;
    }
  }

  function install(): void {
    if (installed || !enabled) {
      return;
    }
    const target =
      options.target ??
      (typeof document === "undefined" ? null : document);
    if (target === null) {
      return;
    }
    const handler = onKeyDown as (event: Event) => void;
    target.addEventListener("keydown", handler);
    detach = (): void => {
      target.removeEventListener("keydown", handler);
    };
    installed = true;
  }

  function uninstall(): void {
    if (!installed) {
      return;
    }
    detach?.();
    detach = null;
    installed = false;
  }

  // Auto-install + auto-cleanup when used inside a component setup().
  install();
  onScopeDispose(() => {
    uninstall();
  });

  return {
    install,
    uninstall,
    isInstalled(): boolean {
      return installed;
    },
  };
}
