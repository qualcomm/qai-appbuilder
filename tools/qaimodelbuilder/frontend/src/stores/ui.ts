// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * UI-global state store.
 *
 * S5 PR-050: holds locale + theme + sidebar collapsed flag.
 * S5 PR-052: adds (additive only — no field renames or removals)
 *            `documentTitleSuffix`, `resolvedTheme` action helpers, and
 *            `setSidebarCollapsed` for symmetry.
 * S5 PR-053: adds (additive only) global UI state migrated out of
 *            legacy frontend/js/app.js (refactor-plan §8.9):
 *              - `fontSize`        — current font-size mode
 *              - `activeToolMode`  — chat-input tool selector mode
 *              - `showToolMessages` — collapse/expand tool messages
 *              - `globalLoading`   — full-screen busy flag
 * 2026-07-20 tool-card bulk toggle: adds (additive) global collapse
 *            override for all `ToolExecPanel` cards, driven by the topbar
 *            "Collapse/Expand Tool Cards" button. Replaces the previous
 *            per-message collapse feature (`messagesCollapsed` +
 *            `collapsedMessageIds` — those were UX misfits: they hid the
 *            entire assistant message body including markdown text, not
 *            just tool cards, which is not what users actually wanted).
 *              - `toolCardsCollapsed`     — 3-state: `null` = user has
 *                                            never used the bulk button,
 *                                            each card follows its own
 *                                            `DEFAULT_COLLAPSED_TOOLS` +
 *                                            running→done auto-collapse
 *                                            defaults; `true`/`false` =
 *                                            user has taken over; every
 *                                            card is forced to that value
 *                                            and its `userToggled` is set
 *                                            so the auto-collapse-on-done
 *                                            watcher stops firing.
 *              - `toolCardsBroadcastTick` — monotonic tick incremented by
 *                                            every `setToolCardsCollapsed`
 *                                            call. `ToolExecPanel` watches
 *                                            this tick so re-clicking the
 *                                            bulk button (same value) also
 *                                            re-applies to any card the
 *                                            user has since manually
 *                                            toggled — see the header-click
 *                                            handler in `ToolExecPanel.vue`.
 *            Persisted to `localStorage` under `TOOL_CARDS_COLLAPSED_KEY`;
 *            once the user clicks the bulk button, that preference is
 *            permanent (no API to reset to `null`), matching the "I want
 *            to see every detail, don't fold anything" mental model.
 */
import { defineStore } from "pinia";

export type AppTheme = "light" | "dark" | "auto";
export type ResolvedTheme = "light" | "dark";
export type AppLocale = "en" | "zh-CN" | "zh-TW";
export type FontSize = "sm" | "md" | "lg" | "xl";
export type ToolMode =
  | null
  | "app-builder"
  | "model-build"
  | "model-hub"
  | "ppt"
  | "code"
  | "translate"
  | "pro"
  | "gomaster";

interface UiState {
  theme: AppTheme;
  locale: AppLocale;
  sidebarCollapsed: boolean;
  /** Mobile sidebar open state — used on small screens (≤768px) where the
   *  sidebar is off-screen by default. Toggled by the topbar hamburger button. */
  mobileSidebarOpen: boolean;
  /**
   * Effective theme after resolving "auto" against
   * `prefers-color-scheme`. Updated by `useTheme()` (PR-052) so views
   * can consume a definitive value without re-querying matchMedia.
   */
  resolvedTheme: ResolvedTheme;
  /** Optional suffix appended to `document.title` by the route guard. */
  documentTitleSuffix: string;
  /** Font-size class. Persisted by callers if needed. */
  fontSize: FontSize;
  /** Active "tool mode" pill in the chat input area. */
  activeToolMode: ToolMode;
  /** Whether tool-call messages are visible in the chat list. */
  showToolMessages: boolean;
  /** Global bulk-collapse for `ToolExecPanel` cards. `null` = user has
   *  never touched the topbar bulk button, each card decides on its own.
   *  `true` / `false` = user has taken over; new cards mount into this
   *  value with `userToggled = true` (see `ToolExecPanel.vue`). Persisted
   *  to localStorage, no API resets it back to `null`. */
  toolCardsCollapsed: boolean | null;
  /** Monotonic broadcast tick — incremented by every
   *  `setToolCardsCollapsed` call so `ToolExecPanel` watchers can force
   *  re-application even when the boolean value did not change (needed
   *  when the user has since manually toggled a single card and then
   *  clicks the bulk button again in the same direction). */
  toolCardsBroadcastTick: number;
  /** Global busy flag (e.g., during reboot / re-login). */
  globalLoading: boolean;
}

const DEFAULT_LOCALE: AppLocale = "en";
// V1 parity (app.js:122 `isDark = ref(true)`): the UI ships dark by default.
// V2 previously defaulted to "auto" (follow system), so on a light OS the app
// opened light — a regression from V1's always-dark experience. We default to
// "dark" and persist the user's explicit choice (see detectInitialTheme).
const DEFAULT_THEME: AppTheme = "dark";

/** localStorage key used to persist the user's explicit locale choice
 *  (V1 parity: LanguageSwitcher writes the same key). */
const LOCALE_STORAGE_KEY = "qai_locale";
/** localStorage key for the user's explicit theme choice. Persisted so a
 *  reload / re-open restores it (V1 never persisted theme — this is a
 *  deliberate enhancement so "set it once" sticks across restarts). */
const THEME_STORAGE_KEY = "qai_theme";
/** localStorage key for the sidebar collapsed state. Persisted so a
 *  reload / re-open restores the user's preferred sidebar width. */
const SIDEBAR_COLLAPSED_KEY = "qai_sidebar_collapsed";
/** localStorage key for the "show tool-call cards" preference (2026-07-20:
 *  previously session-only, now persisted so the Settings → App Config
 *  toggle sticks across reloads/restarts — same pattern as theme/sidebar).
 *  Default `true` when the key is missing (parity with the previous
 *  session-only default). */
const SHOW_TOOL_MESSAGES_KEY = "qai_show_tool_messages";
/** localStorage key for the tool-card bulk-collapse preference. Missing
 *  or invalid = user has never used the topbar bulk button, each card
 *  keeps its own defaults; `"true"` / `"false"` = user has taken over. */
const TOOL_CARDS_COLLAPSED_KEY = "qai_tool_cards_collapsed";

function detectInitialSidebarCollapsed(): boolean {
  try {
    const stored = localStorage.getItem(SIDEBAR_COLLAPSED_KEY);
    if (stored === "true") return true;
    if (stored === "false") return false;
  } catch {
    // localStorage unavailable — fall through to default.
  }
  return false;
}

function detectInitialShowToolMessages(): boolean {
  // Default `true` (visible) when nothing is stored — preserves the prior
  // session-only behaviour on a fresh install. An explicit `"false"` sticks
  // across reloads / restarts once the user flips the Settings toggle.
  try {
    const stored = localStorage.getItem(SHOW_TOOL_MESSAGES_KEY);
    if (stored === "true") return true;
    if (stored === "false") return false;
  } catch {
    // localStorage unavailable — fall through to default.
  }
  return true;
}

function detectInitialToolCardsCollapsed(): boolean | null {
  // Default `null` (per-card defaults) when nothing is stored. Once the
  // user clicks the topbar bulk button the choice sticks across reloads.
  try {
    const stored = localStorage.getItem(TOOL_CARDS_COLLAPSED_KEY);
    if (stored === "true") return true;
    if (stored === "false") return false;
  } catch {
    // localStorage unavailable — fall through to null default.
  }
  return null;
}

function detectInitialTheme(): AppTheme {
  // Explicit user choice persisted to localStorage takes precedence so a
  // reload / re-open restores the selected theme; otherwise default to dark.
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark" || stored === "auto") {
      return stored;
    }
  } catch {
    // localStorage unavailable — fall through to the default.
  }
  return DEFAULT_THEME;
}

function detectInitialLocale(): AppLocale {
  // 1) Explicit user choice persisted to localStorage takes precedence so a
  //    full page reload / direct URL open restores the selected language
  //    (previously this was dropped, causing the UI to fall back to English).
  try {
    const stored = localStorage.getItem(LOCALE_STORAGE_KEY);
    if (stored === "en" || stored === "zh-CN" || stored === "zh-TW") {
      return stored;
    }
  } catch {
    // localStorage unavailable (SSR / privacy mode) — fall through to nav lang.
  }
  // 2) Otherwise fall back to the browser UI language.
  if (typeof navigator === "undefined") {
    return DEFAULT_LOCALE;
  }
  const candidate = navigator.language;
  if (candidate?.startsWith("zh-TW") === true) {
    return "zh-TW";
  }
  if (candidate?.startsWith("zh") === true) {
    return "zh-CN";
  }
  return "en";
}

export const useUiStore = defineStore("ui", {
  state: (): UiState => ({
    theme: detectInitialTheme(),
    locale: detectInitialLocale(),
    sidebarCollapsed: detectInitialSidebarCollapsed(),
    mobileSidebarOpen: false,
    resolvedTheme: "dark",
    documentTitleSuffix: "QAIModelBuilder",
    fontSize: "md",
    activeToolMode: null,
    showToolMessages: detectInitialShowToolMessages(),
    toolCardsCollapsed: detectInitialToolCardsCollapsed(),
    toolCardsBroadcastTick: 0,
    globalLoading: false,
  }),
  actions: {
    setTheme(theme: AppTheme): void {
      this.theme = theme;
      // Persist so the choice survives a reload / re-open (mirrors setLocale).
      try {
        localStorage.setItem(THEME_STORAGE_KEY, theme);
      } catch {
        // localStorage unavailable — keep the in-memory value only.
      }
    },
    setLocale(locale: string): void {
      if (locale === "en" || locale === "zh-CN" || locale === "zh-TW") {
        this.locale = locale;
        // Persist so a reload / direct URL open restores the choice
        // (V1 parity: same localStorage key as LanguageSwitcher).
        try {
          localStorage.setItem(LOCALE_STORAGE_KEY, locale);
        } catch {
          // localStorage unavailable — selection still applies for this session.
        }
      }
    },
    toggleSidebar(): void {
      this.sidebarCollapsed = !this.sidebarCollapsed;
      try {
        localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(this.sidebarCollapsed));
      } catch {
        // localStorage unavailable — keep in-memory value only.
      }
      // Close mobile sidebar when toggling the collapsed state on desktop.
      this.mobileSidebarOpen = false;
    },
    setSidebarCollapsed(collapsed: boolean): void {
      this.sidebarCollapsed = collapsed;
      try {
        localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(collapsed));
      } catch {
        // localStorage unavailable — keep in-memory value only.
      }
    },
    toggleMobileSidebar(): void {
      this.mobileSidebarOpen = !this.mobileSidebarOpen;
    },
    setMobileSidebarOpen(open: boolean): void {
      this.mobileSidebarOpen = open;
    },
    setResolvedTheme(theme: ResolvedTheme): void {
      this.resolvedTheme = theme;
    },
    setDocumentTitleSuffix(suffix: string): void {
      this.documentTitleSuffix = suffix;
    },
    setFontSize(size: FontSize): void {
      this.fontSize = size;
    },
    setActiveToolMode(mode: ToolMode): void {
      this.activeToolMode = mode;
    },
    setShowToolMessages(visible: boolean): void {
      this.showToolMessages = visible;
      try {
        localStorage.setItem(SHOW_TOOL_MESSAGES_KEY, String(visible));
      } catch {
        // localStorage unavailable — keep in-memory value only (session).
      }
    },
    /**
     * Set the global bulk-collapse flag for all `ToolExecPanel` cards.
     * Called by the topbar "Collapse/Expand Tool Cards" button. Method
     * B semantics (chosen 2026-07-20): every card is force-set to this
     * value, its per-card `userToggled` is set to `true` so the
     * running→done auto-collapse watcher stops firing, and the choice
     * persists across reloads via `TOOL_CARDS_COLLAPSED_KEY`. The tick
     * is unconditionally incremented so `ToolExecPanel` watchers re-fire
     * even when the boolean value did not change (matters when the user
     * has since manually toggled a single card and is clicking the bulk
     * button again in the same direction to "reset" that outlier).
     */
    setToolCardsCollapsed(collapsed: boolean): void {
      this.toolCardsCollapsed = collapsed;
      this.toolCardsBroadcastTick += 1;
      try {
        localStorage.setItem(TOOL_CARDS_COLLAPSED_KEY, String(collapsed));
      } catch {
        // localStorage unavailable — keep in-memory value only (session).
      }
    },
    setGlobalLoading(loading: boolean): void {
      this.globalLoading = loading;
    },
  },
});
