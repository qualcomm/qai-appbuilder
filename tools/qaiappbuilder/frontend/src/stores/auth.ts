// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Auth store — SSO session snapshot mirrored from `GET /api/auth/me`.
 *
 * Purpose: give the SPA a single reactive source-of-truth for
 * "is the login gate on? are we signed in? who? when does the session
 * expire?" so router guards, error interceptors, the sidebar user
 * button and the login-prompt modal all read the same value without
 * racing each other on their own `/api/auth/me` fetches.
 *
 * Lifecycle:
 *   - `App.vue::onMounted` calls `refresh()` once at boot, then renders
 *     the UI regardless; if `authEnabled && !authenticated` it flips
 *     `showLoginPrompt=true` so `LoginPrompt.vue` (a non-dismissable
 *     modal) invites the user to sign in — we no longer hard-redirect
 *     the whole page to Okta on load (that felt jarring).
 *   - The router `beforeEach` guard and the 401 interceptor also call
 *     `promptLogin()` instead of navigating away.
 *   - `App.vue` starts a keep-alive timer that calls `renew()` a few
 *     minutes before `expiresAt` so an active user is never kicked out
 *     mid-task.
 *
 * Field-name lock: `authEnabled` / `authenticated` / `user` mirror the
 * server envelope 1:1 (see `AuthMeResponse` in `api/auth.ts`) — renaming
 * either side is a breaking change.
 */
import { defineStore } from "pinia";
import { fetchAuthMe, renewSession, type AuthUser } from "@/api/auth";
import { apiJson } from "@/api";

/**
 * How long after a SUCCESSFUL login a stale in-flight 401 is ignored.
 * Covers responses that were already on the wire when auth completed.
 */
const POST_LOGIN_GRACE_MS = 3_000;

/**
 * Upper bound on how long a sign-in may be considered "in flight".
 *
 * A real sign-in measured ~8s end to end (Okta token exchange → JWKS fetch →
 * QAI Service exchange → LDAP validate), so this must comfortably exceed that
 * while still releasing the gate if the user abandons the popup without
 * completing it.
 */
const LOGIN_FLOW_TIMEOUT_MS = 120_000;

interface AuthState {
  /** Master switch — `settings.auth.enabled` on the server. */
  authEnabled: boolean;
  /** True when the server expects RFC 8628 device-login instead of browser SSO. */
  deviceFlow: boolean;
  /** True when a valid session cookie is present (or gate is disabled). */
  authenticated: boolean;
  /** Signed-in user; `null` when unauthenticated or gate disabled. */
  user: AuthUser | null;
  /**
   * Session expiry as a UNIX epoch in SECONDS (mirror of the cookie
   * `exp`), or `null` when unknown / not applicable. Drives the
   * keep-alive renewal timer in `App.vue`.
   */
  expiresAt: number | null;
  /** True after the first `refresh()` completes (success or graceful fail). */
  loaded: boolean;
  /**
   * True while the login-prompt modal should be shown. Set by
   * `promptLogin()` (from mount gate / guard / 401 interceptor),
   * cleared only by a successful sign-in (full page reload) so there is
   * no "dismiss" path — the gate is mandatory.
   */
  showLoginPrompt: boolean;
  /**
   * The in-flight `refresh()` promise, or `null` when idle. Concurrent
   * callers await THIS promise instead of returning early — the previous
   * "return void when busy" shortcut was a race hazard: a concurrent caller
   * would resume with `authenticated=false` (initial state) while the
   * in-flight fetch was still writing the true values, and its downstream
   * `authEnabled && !authenticated` check would fire on stale data.
   */
  inflight: Promise<void> | null;
  /**
   * Monotonic timestamp (ms) of the last successful authenticated refresh.
   * Used by `promptLogin()` to ignore stale 401 responses that arrive
   * after a fresh login (race between in-flight 401 and iframe auth).
   */
  _lastAuthAt: number;
  /**
   * Monotonic timestamp (ms) at which a sign-in was STARTED (popup opened),
   * or `0` when no sign-in is in flight.
   *
   * `_lastAuthAt` alone cannot suppress the 401s that arrive DURING a
   * sign-in: it is only written once the login succeeds, so every 401 in the
   * window between "popup opened" and "callback completed" saw a stale value
   * and re-raised the login prompt — a second popup on top of the first.
   * A real sign-in takes several seconds (Okta token exchange + JWKS fetch +
   * service exchange + LDAP validate), far longer than the post-login grace
   * period, so the start of the flow has to be tracked separately.
   */
  _loginStartedAt: number;
}

export const useAuthStore = defineStore("auth", {
  state: (): AuthState => ({
    authEnabled: false,
    deviceFlow: false,
    authenticated: false,
    user: null,
    expiresAt: null,
    loaded: false,
    showLoginPrompt: false,
    inflight: null,
    _lastAuthAt: 0,
    _loginStartedAt: 0,
  }),

  getters: {
    /**
     * When true the sidebar user button should render. Same test that
     * `SidebarUserButton.vue` uses in its `v-if`.
     */
    showUserButton: (state): boolean =>
      state.authEnabled && state.authenticated && state.user !== null,

    /**
     * True while an interactive sign-in is still running.
     *
     * Callers use this to skip "session is dead" handling for a 401 that is
     * merely concurrent with the login flow. Bounded by
     * ``LOGIN_FLOW_TIMEOUT_MS`` so an abandoned popup cannot suppress the
     * prompt forever.
     */
    isLoginInFlight: (state): boolean =>
      state._loginStartedAt !== 0
      && Date.now() - state._loginStartedAt < LOGIN_FLOW_TIMEOUT_MS,

    /** First letter of display_name / username, uppercased. `?` on miss. */
    initial: (state): string => {
      const src =
        state.user?.display_name || state.user?.username || "";
      const trimmed = src.trim();
      return trimmed.length > 0 ? trimmed.charAt(0).toUpperCase() : "?";
    },

    /**
     * Seconds until the session expires, or `null` when unknown. Negative
     * when already expired. Consumed by the keep-alive timer.
     */
    secondsUntilExpiry: (state): number | null => {
      if (state.expiresAt === null) return null;
      return state.expiresAt - Math.floor(Date.now() / 1000);
    },

    /** True when the user is authorized to use Model Builder Pro. */
    isMbProAuthorized: (state): boolean =>
      state.user?.is_mb_pro_authorized === true,

    /** True when the LDAP check failed at login (show "service unavailable"). */
    mbProAccessCheckFailed: (state): boolean =>
      state.user?.mb_pro_access_check_failed === true,
  },

  actions: {
    /**
     * Refresh from the server. Concurrent calls dedupe onto the same
     * in-flight promise — every caller resolves at the same moment,
     * with the same state written, so guards / mount hooks / interceptors
     * that all fire in quick succession read consistent values.
     */
    async refresh(): Promise<void> {
      if (this.inflight !== null) {
        return this.inflight;
      }
      const p = (async () => {
        try {
          const me = await fetchAuthMe();
          this.authEnabled = me.auth_enabled;
          this.deviceFlow = me.device_flow === true;
          this.authenticated = me.authenticated;
          this.user = me.user;
          this.expiresAt = me.expires_at ?? null;
          // A fresh, authenticated snapshot clears any stale prompt AND ends
          // the in-flight sign-in window (the flow it was covering is done).
          if (this.authenticated) {
            this.showLoginPrompt = false;
            this._lastAuthAt = Date.now();
            this._loginStartedAt = 0;
          }
        } finally {
          this.loaded = true;
          this.inflight = null;
        }
      })();
      this.inflight = p;
      return p;
    },

    /** Mark a sign-in as started, so 401s during the flow are ignored. */
    beginLogin(): void {
      this._loginStartedAt = Date.now();
    },

    /** Clear the in-flight sign-in marker (cancelled / finished). */
    endLogin(): void {
      this._loginStartedAt = 0;
    },

    /**
     * Show the mandatory login-prompt modal.
     *
     * Suppressed when: the gate is off, the user is already authenticated, a
     * successful refresh completed moments ago, or a sign-in is CURRENTLY in
     * flight. That last case is what produced the duplicate popup: a real
     * sign-in takes ~8s (Okta token + JWKS + service exchange + LDAP), and any
     * request that 401s inside that window used to re-raise the prompt on top
     * of the running flow — two popups, two full logins.
     */
    promptLogin(): void {
      if (!this.authEnabled) return;
      if (this.authenticated) return;
      // A sign-in is in flight — every 401 until it resolves is expected.
      if (this.isLoginInFlight) return;
      // Grace period: ignore stale 401s that arrive right after login.
      if (Date.now() - this._lastAuthAt < POST_LOGIN_GRACE_MS) return;
      this.showLoginPrompt = true;
    },

    /**
     * Extend the current session (slides `exp` forward server-side) and
     * refresh `expiresAt`. Called by the keep-alive timer shortly before
     * expiry. Silently degrades: if renewal fails (e.g. cookie already
     * gone) the next protected request 401s and re-prompts login.
     */
    async renew(): Promise<void> {
      if (!this.authEnabled || !this.authenticated) return;
      try {
        const res = await renewSession();
        if (res.authenticated && typeof res.expires_at === "number") {
          this.expiresAt = res.expires_at;
        } else {
          // Server says we are no longer authenticated → re-prompt.
          this.authenticated = false;
          this.promptLogin();
        }
      } catch {
        // Network hiccup — leave state as-is; the timer will retry, and
        // any real 401 on a business call re-prompts anyway.
      }
    },

    /**
     * Re-check Model Builder Pro authorization without logging out.
     * Updates the session cookie server-side and refreshes local auth state.
     * Called from the Pro toolbar's「刷新权限」button.
     */
    async refreshMbProAccess(): Promise<void> {
      try {
        await apiJson("POST", "/api/mb-pro-session/refresh-access");
        // Force a fresh /api/auth/me fetch by clearing any in-flight dedup
        // so the new cookie (written by refresh-access) is read immediately.
        this.inflight = null;
        await this.refresh();
      } catch {
        // Network failure — leave state as-is; UI shows "service unavailable"
      }
    },

    /**
     * Reset to the "unknown/off" tuple. Used by tests; the real logout
     * flow (SidebarUserButton) calls `logoutSession()` + `rearmAuthGate()`
     * then resets store state directly, so this helper is test-only.
     */
    reset(): void {
      this.authEnabled = false;
      this.authenticated = false;
      this.user = null;
      this.expiresAt = null;
      this.loaded = false;
      this.showLoginPrompt = false;
      this.inflight = null;
      this._lastAuthAt = 0;
    },
  },
});
