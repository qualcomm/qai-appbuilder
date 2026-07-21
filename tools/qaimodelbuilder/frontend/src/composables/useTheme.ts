// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Theme composable.
 *
 * S5 PR-052: subscribes to `prefers-color-scheme` and writes the
 * effective theme back to the UI store + the document root. Used once
 * globally by `App.vue`.
 *
 * Token contract (PR-802a, see styles/variables.css + base.css): the
 * design tokens default to DARK on `:root`, and the LIGHT theme is an
 * override gated on the `html.light` class. The legacy `[data-theme]`
 * attribute is now a no-op for tokens. Therefore the effective switch
 * is toggling the `light` class on <html>; we also keep writing
 * `data-theme` for any remaining `[data-theme="dark"]` hooks and for
 * debuggability.
 *
 * Why on <html> and not <body>: the `html.light` token selectors live
 * on the root, and it avoids a flash of unthemed content during route
 * transitions.
 *
 * The composable is idempotent — calling `useTheme()` from multiple
 * components reuses the same matchMedia listener but each invocation
 * adds its own teardown, so cleanup is safe.
 */
import { onBeforeUnmount, watchEffect } from "vue";
import { storeToRefs } from "pinia";
import { useUiStore, type ResolvedTheme, type AppTheme } from "@/stores/ui";

const PREFERS_DARK_QUERY = "(prefers-color-scheme: dark)";

function detectSystemTheme(): ResolvedTheme {
  if (
    typeof window === "undefined" ||
    typeof window.matchMedia !== "function"
  ) {
    return "light";
  }
  return window.matchMedia(PREFERS_DARK_QUERY).matches ? "dark" : "light";
}

function resolve(theme: AppTheme): ResolvedTheme {
  if (theme === "auto") {
    return detectSystemTheme();
  }
  return theme;
}

function applyDocumentTheme(theme: ResolvedTheme): void {
  if (typeof document === "undefined") {
    return;
  }
  const root = document.documentElement;
  // The light theme tokens are gated on `html.light` (variables.css:183 +
  // the `html.light .xxx` overrides). Default `:root` is dark. So the real
  // switch is the `light` class; `data-theme` is kept for the legacy
  // `[data-theme="dark"]` hooks and debuggability.
  root.classList.toggle("light", theme === "light");
  root.setAttribute("data-theme", theme);
}

export function useTheme(): {
  theme: ReturnType<typeof storeToRefs<ReturnType<typeof useUiStore>>>["theme"];
  resolvedTheme: ReturnType<
    typeof storeToRefs<ReturnType<typeof useUiStore>>
  >["resolvedTheme"];
  setTheme: (theme: AppTheme) => void;
  cycleTheme: () => void;
} {
  const ui = useUiStore();
  const { theme, resolvedTheme } = storeToRefs(ui);

  let mediaList: MediaQueryList | null = null;
  const onSystemChange = (): void => {
    if (ui.theme === "auto") {
      const resolved = detectSystemTheme();
      ui.setResolvedTheme(resolved);
      applyDocumentTheme(resolved);
    }
  };

  if (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function"
  ) {
    mediaList = window.matchMedia(PREFERS_DARK_QUERY);
    if (typeof mediaList.addEventListener === "function") {
      mediaList.addEventListener("change", onSystemChange);
    }
  }

  watchEffect(() => {
    const resolved = resolve(ui.theme);
    ui.setResolvedTheme(resolved);
    applyDocumentTheme(resolved);
  });

  onBeforeUnmount(() => {
    if (mediaList !== null && typeof mediaList.removeEventListener === "function") {
      mediaList.removeEventListener("change", onSystemChange);
    }
  });

  function setTheme(next: AppTheme): void {
    ui.setTheme(next);
  }

  function cycleTheme(): void {
    const order: AppTheme[] = ["light", "dark", "auto"];
    const idx = order.indexOf(ui.theme);
    const nextIdx = (idx + 1) % order.length;
    const next = order[nextIdx] ?? "auto";
    ui.setTheme(next);
  }

  return { theme, resolvedTheme, setTheme, cycleTheme };
}
