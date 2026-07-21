// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useConfirm` — global confirmation dialog composable.
 *
 * V1 parity (`app.js:showConfirm`): returns a `Promise<boolean>` that
 * resolves `true` on confirm and `false` on cancel/dismiss. The pending
 * resolver lives module-scoped so the single global `<ConfirmDialog>`
 * host (mounted in `App.vue`) can resolve it on user action.
 */
import { useConfirmStore, type ConfirmOptions } from "@/stores/confirm";

let pendingResolve: ((value: boolean) => void) | null = null;

export function useConfirm() {
  const store = useConfirmStore();

  function confirm(opts: ConfirmOptions): Promise<boolean> {
    // If a previous prompt is somehow still pending, resolve it false.
    if (pendingResolve !== null) {
      pendingResolve(false);
      pendingResolve = null;
    }
    store.open(opts);
    return new Promise<boolean>((resolve) => {
      pendingResolve = resolve;
    });
  }

  /** Called by the global host when the user confirms. */
  function accept(): void {
    store.close();
    if (pendingResolve !== null) {
      pendingResolve(true);
      pendingResolve = null;
    }
  }

  /** Called by the global host when the user cancels / dismisses. */
  function cancel(): void {
    store.close();
    if (pendingResolve !== null) {
      pendingResolve(false);
      pendingResolve = null;
    }
  }

  return { confirm, accept, cancel };
}
