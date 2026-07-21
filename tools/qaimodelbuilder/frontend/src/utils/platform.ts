// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * platform — runtime detection of the host shell (Tauri desktop vs. browser).
 *
 * Single source of truth for "are we running inside the Tauri desktop shell
 * or in a plain browser (WebUI)?". This drives platform-specific keyboard
 * shortcut wiring in App.vue:
 *
 *   - Desktop (Tauri 2 + WebView2): the shell installs no native menu and no
 *     accelerators (desktop/src-tauri/src/lib.rs registers only a
 *     `CloseRequested` window handler), so `Ctrl/Cmd+W` and `Ctrl/Cmd+N` are
 *     delivered to the page as ordinary keydown events and `preventDefault()`
 *     reliably suppresses any default. We can therefore bind them to
 *     close/open the in-app chat tab.
 *
 *   - Browser (WebUI): `Ctrl+W` (close tab) and `Ctrl+N` (new window) are
 *     reserved browser/OS shortcuts that JavaScript CANNOT intercept —
 *     `preventDefault()` is ignored for them by Chrome/Edge/Firefox as a
 *     security measure. So in the browser we bind the *alternative* keys
 *     `Alt+W` / `Alt+N`, which are interceptable.
 *
 * Detection: Tauri 2 always injects `window.__TAURI_INTERNALS__` into the
 * webview's global scope, even when `app.withGlobalTauri` is false (as it is
 * in tauri.conf.json). This is the documented, reliable marker. We do not rely
 * on `window.__TAURI__` (only present when `withGlobalTauri: true`).
 */

interface TauriGlobals {
  readonly __TAURI_INTERNALS__?: unknown;
  readonly __TAURI__?: unknown;
}

/**
 * True when the SPA is hosted inside the Tauri desktop shell.
 *
 * Evaluated lazily and cached: the global marker is present from the first
 * script execution, so a single read at module init is sufficient, but we
 * guard against SSR / non-window environments (tests) returning a stable
 * `false`.
 */
function detectDesktopShell(): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  const g = window as unknown as TauriGlobals;
  return (
    g.__TAURI_INTERNALS__ !== undefined || g.__TAURI__ !== undefined
  );
}

let cached: boolean | null = null;

export function isDesktopShell(): boolean {
  if (cached === null) {
    cached = detectDesktopShell();
  }
  return cached;
}

/**
 * Test-only hook to reset the memoized detection between cases.
 * Not used in production code paths.
 */
export function __resetPlatformCacheForTests(): void {
  cached = null;
}
