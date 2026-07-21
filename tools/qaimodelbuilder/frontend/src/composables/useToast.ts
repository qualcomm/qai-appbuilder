// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Toast composable.
 *
 * S5 PR-052: thin wrapper over `useToastStore` that owns
 * auto-dismiss timers. The store stays serialisable; this composable
 * is the only place that holds setTimeout handles, so tests can drive
 * the queue deterministically with `vi.useFakeTimers()` (the timer is
 * a pure browser API) and `useToastStore` resets cleanly between
 * tests via `setActivePinia`.
 *
 * Default timeout is 4.5s (V1 parity: DEFAULT_DURATION = 4500); pass
 * `timeoutMs: 0` for sticky toasts.
 */
import { useToastStore, type ToastKind } from "@/stores/toast";

/** V1 parity: useToast.js DEFAULT_DURATION = 4500ms. */
const DEFAULT_TIMEOUT_MS = 4500;

/**
 * Kind → leading emoji icon (V1 parity: useToast.js `icons` map).
 * Used when a caller does not supply an explicit `icon`.
 */
const KIND_ICONS: Record<ToastKind, string> = {
  success: "✅",
  error: "❌",
  info: "ℹ️",
  warning: "⚠️",
};

let counter = 0;
function nextId(): string {
  if (
    typeof globalThis.crypto !== "undefined" &&
    typeof globalThis.crypto.randomUUID === "function"
  ) {
    return globalThis.crypto.randomUUID();
  }
  counter += 1;
  return `toast-${counter.toString()}`;
}

const timers = new Map<string, ReturnType<typeof setTimeout>>();

export interface PushToastInput {
  kind?: ToastKind;
  message: string;
  timeoutMs?: number;
  /** Optional explicit icon; falls back to a kind-based default. */
  icon?: string;
}

export function useToast(): {
  push: (input: PushToastInput) => string;
  info: (message: string, timeoutMs?: number) => string;
  success: (message: string, timeoutMs?: number) => string;
  warning: (message: string, timeoutMs?: number) => string;
  error: (message: string, timeoutMs?: number) => string;
  dismiss: (id: string) => void;
  clear: () => void;
} {
  const store = useToastStore();

  function dismiss(id: string): void {
    const timer = timers.get(id);
    if (timer !== undefined) {
      clearTimeout(timer);
      timers.delete(id);
    }
    store.dismiss(id);
  }

  function push(input: PushToastInput): string {
    const id = nextId();
    const kind = input.kind ?? "info";
    const timeoutMs = input.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    store.push({
      id,
      kind,
      message: input.message,
      timeoutMs,
      icon: input.icon ?? KIND_ICONS[kind],
    });
    if (timeoutMs > 0) {
      const timer = setTimeout(() => {
        dismiss(id);
      }, timeoutMs);
      timers.set(id, timer);
    }
    return id;
  }

  function clear(): void {
    for (const timer of timers.values()) {
      clearTimeout(timer);
    }
    timers.clear();
    store.clear();
  }

  return {
    push,
    info: (message, timeoutMs) => push({ kind: "info", message, timeoutMs }),
    success: (message, timeoutMs) =>
      push({ kind: "success", message, timeoutMs }),
    warning: (message, timeoutMs) =>
      push({ kind: "warning", message, timeoutMs }),
    error: (message, timeoutMs) => push({ kind: "error", message, timeoutMs }),
    dismiss,
    clear,
  };
}
