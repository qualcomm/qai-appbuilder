// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Router guards.
 *
 * S5 PR-052: sets `document.title` from `route.meta.titleKey` (a vue-i18n
 * message key) on every navigation.
 *
 * PR-SSO: adds a `beforeEach` guard that, when the Okta SSO gate is
 * enabled and the user is not authenticated, shows the in-app login
 * prompt modal (`auth.promptLogin()`) and STILL allows the navigation
 * to proceed — the SPA renders normally behind the modal. We no longer
 * hard-redirect the whole page to Okta (that felt jarring). When
 * `auth_enabled=false` the guard is a no-op.
 *
 * The guard is installed via `installGuards(router, i18nLike)` from
 * `App.vue` (PR-052) so the dependency direction stays linear (router
 * depends on i18n, never the other way round) and `main.ts` stays
 * frozen as PR-050 mandated.
 *
 * `I18nLike` is a duck-typed contract — any object exposing
 * `{ t(key: string): string }` works, which covers both
 * `i18n.global` (returned by `createI18n`) and the local Composer
 * returned by `useI18n()` inside a setup function.
 */
import type { Router, RouteLocationNormalized } from "vue-router";

import { useAuthStore } from "@/stores/auth";

export interface I18nLike {
  t: (key: string) => string;
}

interface RouteMetaWithTitle {
  titleKey?: string;
}

export interface GuardOptions {
  /** Override base title; defaults to `app.title`. */
  baseTitleKey?: string;
}

function resolveTitle(
  i18n: I18nLike,
  route: RouteLocationNormalized,
  baseTitleKey: string,
): string {
  const meta = (route.meta ?? {}) as RouteMetaWithTitle;
  const base = i18n.t(baseTitleKey);
  if (meta.titleKey === undefined) {
    return base;
  }
  const view = i18n.t(meta.titleKey);
  if (view === meta.titleKey || view === "") {
    return base;
  }
  return `${view} · ${base}`;
}

export function installGuards(
  router: Router,
  i18n: I18nLike,
  options: GuardOptions = {},
): void {
  const baseTitleKey = options.baseTitleKey ?? "app.title";

  // SSO login gate. Runs before every navigation. Idempotent: the auth
  // store's own in-flight dedup suppresses overlapping fetches. When the
  // gate is on and no valid session exists, we show the in-app login
  // prompt modal but STILL allow navigation (the SPA renders behind it).
  // `authEnabled=false` default means an unconfigured backend produces
  // zero navigation side-effects.
  router.beforeEach(async (_to) => {
    const auth = useAuthStore();
    if (!auth.loaded) {
      await auth.refresh();
    }
    if (auth.authEnabled && !auth.authenticated) {
      auth.promptLogin();
    }
    return true;
  });

  router.afterEach((to) => {
    if (typeof document === "undefined") {
      return;
    }
    document.title = resolveTitle(i18n, to, baseTitleKey);
  });
}

/** Exposed for unit tests — same logic, no router required. */
export function computeRouteTitle(
  i18n: I18nLike,
  route: RouteLocationNormalized,
  baseTitleKey = "app.title",
): string {
  return resolveTitle(i18n, route, baseTitleKey);
}
