// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Command palette store.
 *
 * S5 PR-052: tracks open/close state and the current query string.
 * The list of registered commands is computed in
 * `useCommandPalette.ts` (composable) — keeping it out of the store
 * lets command sources stay pure functions of the i18n locale and the
 * route table.
 */
import { defineStore } from "pinia";

interface CommandPaletteState {
  open: boolean;
  query: string;
}

export const useCommandPaletteStore = defineStore("commandPalette", {
  state: (): CommandPaletteState => ({
    open: false,
    query: "",
  }),
  actions: {
    show(): void {
      this.open = true;
    },
    hide(): void {
      this.open = false;
      this.query = "";
    },
    toggle(): void {
      if (this.open) {
        this.hide();
      } else {
        this.show();
      }
    },
    setQuery(value: string): void {
      this.query = value;
    },
  },
});
