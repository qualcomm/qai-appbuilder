// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Auth API — thin client for the Okta OIDC SSO surface exposed by the
 * backend at `interfaces/http/routes/auth.py`.
 *
 * The login gate itself is entirely server-driven (top-level browser
 * navigation to `/auth/login` → Okta → `/callback`); this module only
 * covers the JSON introspection endpoint (`/api/auth/me`) the SPA uses
 * to decide whether to render the sidebar user button.
 *
 * All navigation-triggering helpers (`redirectToLogin`, `logout`) use
 * `window.location.href` on purpose — the SSO flow MUST leave the SPA
 * (Okta rejects XHR authorize calls), and using `location.href` gives
 * the browser a full document reload, dropping the in-memory SPA state
 * so a stale `/auth/me` cache can never survive across a login boundary.
 */

import { apiJson } from "./http";

/** Snapshot of the currently signed-in user, projected server-side. */
export interface AuthUser {
  readonly username: string;
  readonly email: string;
  readonly name: string;
  readonly display_name: string;
  readonly sub: string;
  readonly auth_source: string;
  /** True when the user is authorized to use Model Builder Pro. */
  readonly is_mb_pro_authorized: boolean;
  /** True when the LDAP membership check failed at login (service unavailable). */
  readonly mb_pro_access_check_failed: boolean;
}

/** Response shape of `GET /api/auth/me`. */
export interface AuthMeResponse {
  /**
   * Feature master switch (mirror of `settings.auth.enabled`). When
   * `false` the login gate is disabled — the SPA should render exactly
   * as if this feature did not exist.
   */
  readonly auth_enabled: boolean;
  /**
   * Whether the current request carried a valid session cookie. Always
   * `true` when `auth_enabled=false` (every caller is implicitly "in").
   */
  readonly authenticated: boolean;
  /** `null` when unauthenticated or when the feature is disabled. */
  readonly user: AuthUser | null;
  /**
   * Session expiry as a UNIX epoch in SECONDS (mirror of the session
   * cookie `exp`), or `null` when unauthenticated / gate disabled.
   * Drives the client-side keep-alive renewal timer.
   */
  readonly expires_at?: number | null;
}

/** Response shape of `POST /api/auth/renew`. */
export interface AuthRenewResponse {
  readonly authenticated: boolean;
  /** New expiry (UNIX seconds) after the slide, or `null` on failure. */
  readonly expires_at?: number | null;
}

/**
 * Fetch the auth snapshot from the backend. Never throws — a network
 * error just yields the "unknown" tuple `{auth_enabled: false,
 * authenticated: false, user: null}` so callers can render the SPA
 * without gating logic branching on a rejected promise.
 */
export async function fetchAuthMe(
  signal?: AbortSignal,
): Promise<AuthMeResponse> {
  try {
    return await apiJson<AuthMeResponse>(
      "GET",
      "/api/auth/me",
      undefined,
      { signal },
    );
  } catch {
    // Any failure (network, malformed JSON) → treat as "auth off" so the
    // SPA still renders. If the gate is truly on, the next protected
    // request will 401 and the login prompt will take over.
    return { auth_enabled: false, authenticated: false, user: null };
  }
}

/**
 * Extend ("slide") the current session's expiry server-side. Returns the
 * new `expires_at`. The caller (auth store `renew()`) handles failures;
 * this only surfaces the parsed response.
 */
export function renewSession(
  signal?: AbortSignal,
): Promise<AuthRenewResponse> {
  return apiJson<AuthRenewResponse>(
    "POST",
    "/api/auth/renew",
    undefined,
    { signal },
  );
}

/**
 * Navigate to the backend login endpoint with a `next` return path.
 * Full document reload on purpose — see module docstring.
 */
export function redirectToLogin(nextPath?: string): void {
  const next = nextPath ?? window.location.pathname + window.location.search;
  const url = `/auth/login?next=${encodeURIComponent(next)}`;
  window.location.href = url;
}

/**
 * Attempt silent re-authentication via a hidden iframe.
 *
 * When the IdP (Okta) session is still valid, the entire redirect chain
 * (`/auth/login` → Okta `/authorize` → `/callback` → `/auth/popup-done`)
 * completes as a series of 302 redirects WITHOUT rendering the Okta login
 * page. Since no cross-origin page is rendered, `frame-ancestors` / COOP
 * restrictions do not apply, and the iframe reaches `/auth/popup-done`
 * which posts `qai_auth_complete` to `window.parent`.
 *
 * If the IdP session has expired, Okta will try to render its login page
 * inside the iframe — that will either be blocked by CSP or simply not
 * complete within the timeout. Either way we reject, and the caller falls
 * back to `popupLogin()`.
 *
 * @param opts.timeoutMs   How long to wait for the silent flow (default 4000ms).
 * @param opts.reexchange  When true, force a full Okta round-trip even if a
 *   valid session cookie is present (`/auth/login?reexchange=1`). Needed to
 *   RE-MINT the QAI Service pool JWT: it is only minted inside `/callback`, and
 *   `/auth/login` normally short-circuits straight to `next` when already
 *   signed in — so without this flag the iframe would resolve "successfully"
 *   without ever hitting `/callback`, and no new token would be exchanged.
 *   Defaults to false so the ordinary login path keeps its fast short-circuit.
 * @returns Resolves if silent auth succeeded; rejects otherwise.
 */
export function silentLogin(
  opts: { timeoutMs?: number; reexchange?: boolean } = {},
): Promise<void> {
  const { timeoutMs = 4000, reexchange = false } = opts;
  return new Promise<void>((resolve, reject) => {
    const params = new URLSearchParams({ next: "/auth/popup-done" });
    if (reexchange) params.set("reexchange", "1");
    const loginUrl = `/auth/login?${params.toString()}`;

    const iframe = document.createElement("iframe");
    iframe.style.display = "none";
    iframe.setAttribute("aria-hidden", "true");

    let settled = false;

    function cleanup(): void {
      window.removeEventListener("message", onMessage);
      clearTimeout(timer);
      // Small delay before removing iframe to let cookie settle
      setTimeout(() => iframe.remove(), 200);
    }

    function onMessage(event: MessageEvent): void {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== "qai_auth_complete") return;
      if (settled) return;
      settled = true;
      cleanup();
      resolve();
    }

    window.addEventListener("message", onMessage);

    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(new Error("silent_timeout"));
    }, timeoutMs);

    // NOTE: no ``iframe.addEventListener('error', ...)`` here — for the two
    // ways silent auth actually fails (X-Frame-Options DENY on the Okta
    // login page, CSP frame-ancestors block, or a stalled 3rd-party
    // redirect chain) the browser does NOT fire ``error`` on the iframe:
    // XFO-blocked frames simply render blank, and blocked navigations are
    // reported to the console, not to the JS event target. The timeout
    // above is the sole reliable failure signal; adding an ``error`` handler
    // was misleading dead code.

    iframe.src = loginUrl;
    document.body.appendChild(iframe);
  });
}

/**
 * Open the Okta login flow in a popup window. The popup navigates:
 *   `/auth/login` → Okta → `/callback` → `/auth/popup-done`
 * which posts a message back to the opener (this window).
 *
 * Returns a Promise that resolves when auth completes successfully.
 * Rejects with `Error("popup_closed")` if the user closes the popup.
 * Rejects with `Error("popup_blocked")` if the browser blocks the popup.
 *
 * The main window shows a theme-aware overlay with a spinner while
 * the popup is open, telling the user to complete login there.
 *
 * Fallback: if popup is blocked, caller can choose to `redirectToLogin()`.
 */
export function popupLogin(): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const loginUrl = `/auth/login?next=${encodeURIComponent("/auth/popup-done")}`;

    // Open popup SYNCHRONOUSLY in the user-click context to avoid blockers.
    const popup = window.open(
      loginUrl,
      "qai_login",
      "popup=yes,width=520,height=700,left=200,top=100",
    );

    if (!popup || popup.closed) {
      console.warn("[auth:popup] popup blocked by browser");
      reject(new Error("popup_blocked"));
      return;
    }

    // --- Overlay on the main window ---
    const themeEl = document.querySelector("[data-theme]");
    const isDark = themeEl?.getAttribute("data-theme") === "dark";

    const overlay = document.createElement("div");
    overlay.id = "qai-auth-popup-overlay";
    overlay.style.cssText = [
      "position:fixed", "inset:0", "z-index:9600",
      "display:flex", "flex-direction:column",
      "align-items:center", "justify-content:center", "gap:20px",
      "background:rgba(0,0,0,0.6)", "backdrop-filter:blur(4px)",
      "-webkit-backdrop-filter:blur(4px)",
      "font-family:system-ui,sans-serif",
    ].join(";");

    // Spinner
    const spinner = document.createElement("div");
    spinner.innerHTML = `
      <svg width="40" height="40" viewBox="0 0 24 24" fill="none" style="animation:qai-spin 1s linear infinite">
        <circle cx="12" cy="12" r="10" stroke="${isDark ? "#4b5563" : "#d1d5db"}" stroke-width="2.5"/>
        <path d="M12 2a10 10 0 0 1 10 10" stroke="${isDark ? "#818cf8" : "#4f46e5"}" stroke-width="2.5" stroke-linecap="round"/>
      </svg>
      <style>@keyframes qai-spin{to{transform:rotate(360deg)}}</style>
    `;
    overlay.appendChild(spinner);

    // Message
    const msg = document.createElement("div");
    msg.style.cssText = [
      `color:${isDark ? "#e2e8f0" : "#1f2937"}`,
      "font-size:16px", "font-weight:500",
      "text-align:center",
    ].join(";");
    msg.textContent = "Please complete sign-in in the popup window";
    overlay.appendChild(msg);

    // Sub-message
    const sub = document.createElement("div");
    sub.style.cssText = [
      `color:${isDark ? "#94a3b8" : "#6b7280"}`,
      "font-size:13px", "text-align:center",
    ].join(";");
    sub.textContent = "This page will update automatically once you sign in.";
    overlay.appendChild(sub);

    // Cancel button
    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "Cancel";
    cancelBtn.style.cssText = [
      "margin-top:12px", "padding:8px 24px",
      "border-radius:8px", "border:none",
      `background:${isDark ? "rgba(255,255,255,0.1)" : "rgba(0,0,0,0.06)"}`,
      `color:${isDark ? "#cbd5e1" : "#374151"}`,
      "font-size:14px", "cursor:pointer",
      "transition:background 0.15s",
    ].join(";");
    cancelBtn.addEventListener("mouseenter", () => {
      cancelBtn.style.background = isDark ? "rgba(255,255,255,0.18)" : "rgba(0,0,0,0.1)";
    });
    cancelBtn.addEventListener("mouseleave", () => {
      cancelBtn.style.background = isDark ? "rgba(255,255,255,0.1)" : "rgba(0,0,0,0.06)";
    });
    overlay.appendChild(cancelBtn);

    document.body.appendChild(overlay);

    let settled = false;

    function complete(): void {
      if (settled) return;
      settled = true;
      cleanup();
      resolve();
    }

    function cancel(): void {
      if (settled) return;
      settled = true;
      if (popup && !popup.closed) popup.close();
      cleanup();
      reject(new Error("popup_closed"));
    }

    // Listen for postMessage from /auth/popup-done
    function onMessage(event: MessageEvent): void {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== "qai_auth_complete") return;
      // Verify source is our popup (when accessible)
      if (event.source && event.source !== popup) return;
      if (popup && !popup.closed) popup.close();
      complete();
    }

    // Poll for popup closure (user closed it manually).
    // Wrapped in try/catch because Cross-Origin-Opener-Policy on the IdP
    // page may block access to `popup.closed`, producing console warnings.
    // In that case we rely solely on postMessage for completion signaling.
    const pollInterval = window.setInterval(() => {
      try {
        if (popup.closed && !settled) {
          cancel();
        }
      } catch {
        // COOP blocks access — ignore; postMessage is the primary signal.
      }
    }, 1000);

    // Cancel button & ESC
    cancelBtn.addEventListener("click", cancel);
    function onKeydown(e: KeyboardEvent): void {
      if (e.key === "Escape") cancel();
    }
    document.addEventListener("keydown", onKeydown);

    function cleanup(): void {
      window.removeEventListener("message", onMessage);
      document.removeEventListener("keydown", onKeydown);
      clearInterval(pollInterval);
      overlay.remove();
    }

    window.addEventListener("message", onMessage);
  });
}


/**
 * Sign out without leaving the SPA.  Calls the backend logout endpoint
 * (which clears the session cookie) via fetch, then returns.
 *
 * The caller is responsible for updating the auth store state (e.g.
 * `auth.authenticated = false; auth.showLoginPrompt = true`).
 * This avoids a circular import (stores/auth imports from api/auth).
 *
 * Uses `redirect: "manual"` — we don't want to follow the 303 to
 * /auth/signed-out (that's a standalone HTML page for non-SPA usage).
 */
export async function logoutSession(): Promise<void> {
  try {
    await fetch("/auth/logout", { redirect: "manual", credentials: "same-origin" });
  } catch {
    // Network error — cookie may already be cleared; proceed anyway.
  }
}
