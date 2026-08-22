// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Confirm dialog store.
 *
 * V1 parity (`app.js:showConfirm` + `.confirm-overlay`/`.confirm-dialog`):
 * a single global confirmation dialog driven by a reactive state object.
 * `useConfirm().confirm(opts)` returns a `Promise<boolean>` that resolves
 * `true` when the user confirms and `false` when they cancel/dismiss.
 *
 * The resolver closure is held in the composable (not the store) so the
 * store stays serialisable, matching the `toast` store's split.
 */
import { defineStore } from "pinia";

export type ConfirmStyle = "primary" | "danger";

export interface ConfirmOptions {
  title: string;
  message: string;
  confirmText?: string;
  cancelText?: string;
  confirmStyle?: ConfirmStyle;
  icon?: string;
}

interface ConfirmState {
  visible: boolean;
  icon: string;
  title: string;
  message: string;
  confirmText: string;
  cancelText: string;
  confirmStyle: ConfirmStyle;
}

function initialState(): ConfirmState {
  return {
    visible: false,
    icon: "",
    title: "",
    message: "",
    confirmText: "Confirm",
    cancelText: "Cancel",
    confirmStyle: "primary",
  };
}

export const useConfirmStore = defineStore("confirm", {
  state: (): ConfirmState => initialState(),
  actions: {
    open(opts: ConfirmOptions): void {
      this.visible = true;
      this.icon = opts.icon ?? "";
      this.title = opts.title;
      this.message = opts.message;
      this.confirmText = opts.confirmText ?? "Confirm";
      this.cancelText = opts.cancelText ?? "Cancel";
      this.confirmStyle = opts.confirmStyle ?? "primary";
    },
    close(): void {
      this.visible = false;
    },
  },
});
