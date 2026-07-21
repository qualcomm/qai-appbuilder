// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Header actions store — V1 parity for the topbar right-side button group.
 *
 * Background
 * ----------
 * V1 puts page-specific actions (refresh / settings / new-conversation /
 * clear / export …) directly on the global topbar (index.html:316-397,
 * `<div class="header-actions" v-if="currentView === '…'">` blocks).
 *
 * V2's <AppHeader> originally exposed a `<slot name="toolbar-actions" />`,
 * but `<AppHeader>` and the `<RouterView>` are siblings under
 * <App.vue> (see `App.vue:96-104`), so a child view CANNOT reach that slot
 * via Vue template syntax — the slot is dead code. As a result every page
 * except chat ended up rendering its own in-page panel-header, so the
 * buttons appeared in the *body* instead of the *topbar*, drifting from
 * the V1 user-perceived layout.
 *
 * Solution
 * --------
 * A small Pinia-style store + composable. Each view registers the
 * actions it wants in the topbar via `useHeaderActions(() => [...])`
 * (see `composables/useHeaderActions.ts`). `<AppHeader>` reads
 * `useHeaderActionsStore().actions` and renders them with v-for. The
 * composable clears the actions on `onBeforeUnmount` so navigating
 * away leaves the topbar clean.
 *
 * Design notes
 * ------------
 *  - We deliberately keep it as a `defineStore` (Pinia) so the registry
 *    is testable + the same instance is reachable from any component
 *    (header / router).
 *  - `HeaderAction` carries either an `icon` (emoji string) or
 *    `iconSvg` (raw SVG markup). The latter is only used by the chat
 *    pills which keep their inline SVGs from V1 for pixel-perfect
 *    parity (V1 index.html:318-337). Consumers MUST pre-sanitize SVG
 *    strings — they're emitted via `v-html`.
 */
import { defineStore } from "pinia";

/**
 * One header action. The shape is intentionally narrow:
 * the actions array is rebuilt on every reactivity tick by the
 * registering composable, so identity is keyed by `id` rather than
 * by reference.
 */
export interface HeaderAction {
  /** Stable key (e.g. `"service.refresh"`, `"chat.workspace"`). */
  id: string;
  /** Already-translated label (call `t()` in the registering view). */
  label: string;
  /**
   * Emoji (or other plain text glyph) to render before the label,
   * e.g. `"🔄"`, `"⚙️"`. Mutually exclusive with `iconSvg`.
   */
  icon?: string;
  /**
   * Raw SVG markup — used only by the chat toolbar pills which need
   * pixel-perfect V1 SVG icons. Emitted via `v-html`; consumers MUST
   * pre-sanitize.
   */
  iconSvg?: string;
  /** `title` (tooltip). Falls back to `label` when omitted. */
  title?: string;
  /**
   * Visual style. `ghost` is the default outlined toolbar button.
   * `primary` is the chat "新建对话" highlight (V1 index.html:386).
   */
  variant?: "ghost" | "primary";
  /** Whether the button is currently disabled. */
  disabled?: boolean;
  /** Click handler. Async return is awaited in the template. */
  onClick: () => void | Promise<void>;
  /** Optional `data-testid` to preserve existing e2e selectors. */
  testId?: string;
  /**
   * Optional aria-pressed (used by toggle pills like "Tool Calls" and
   * "Collapse All" — V1 index.html:319, 329). When present the button
   * also gets an `active` class for V1 visual parity.
   */
  pressed?: boolean;
  /**
   * Optional extra classes to layer onto the button. Used by chat
   * pills which carry the legacy `chat-toolbar-toggle` class for V1
   * pill styling.
   */
  extraClass?: string;
  /**
   * When true, the action is rendered inside the "⋯" overflow menu
   * instead of directly in the topbar. Used for low-frequency actions
   * (e.g. export, clear) to reduce toolbar clutter.
   */
  overflow?: boolean;
}

interface HeaderActionsState {
  actions: HeaderAction[];
}

/**
 * Pinia store that holds the current view's header action list.
 *
 * Mutation discipline: only `set` and `clear` — the registering
 * composable replaces the array atomically each tick. Components
 * MUST NOT push/splice into `actions` directly.
 */
export const useHeaderActionsStore = defineStore("headerActions", {
  state: (): HeaderActionsState => ({
    actions: [],
  }),
  actions: {
    /** Replace the registered actions. Called on every reactive tick. */
    set(actions: HeaderAction[]): void {
      this.actions = actions;
    },
    /** Drop all actions (called on view unmount). */
    clear(): void {
      this.actions = [];
    },
  },
});
