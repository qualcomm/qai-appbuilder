// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Toast notification store.
 *
 * S5 PR-052: minimal toast queue consumed by `AppToastHost.vue`.
 * Each toast carries an id, kind (info/success/warning/error), a
 * message (already-translated), and an optional auto-dismiss timeout
 * in milliseconds. Auto-dismiss timers are managed by `useToast()`.
 *
 * The store deliberately stays serialisable (no closures): timer
 * management lives in the composable so tests can drive the queue
 * deterministically with `vi.useFakeTimers`.
 */
import { defineStore } from "pinia";

export type ToastKind = "info" | "success" | "warning" | "error";

export interface Toast {
  id: string;
  kind: ToastKind;
  message: string;
  /** Auto-dismiss timeout in ms; 0 means "sticky until dismissed". */
  timeoutMs: number;
  /**
   * Leading icon (emoji) shown before the message. Optional on input;
   * `useToast()` fills a kind-based default (V1 parity) when omitted.
   */
  icon?: string;
}

interface ToastState {
  items: Toast[];
}

export const useToastStore = defineStore("toast", {
  state: (): ToastState => ({
    items: [],
  }),
  actions: {
    push(toast: Toast): void {
      this.items = [...this.items, toast];
    },
    dismiss(id: string): void {
      this.items = this.items.filter((t) => t.id !== id);
    },
    clear(): void {
      this.items = [];
    },
  },
});
