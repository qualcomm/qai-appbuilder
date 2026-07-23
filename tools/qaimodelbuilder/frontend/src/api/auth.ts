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

/** Navigate to the backend logout endpoint. Full document reload. */
export function redirectToLogout(): void {
  window.location.href = "/auth/logout";
}
