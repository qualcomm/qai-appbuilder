// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * usePermissionAlert — background-tab attention cue for pending
 * authorization requests.
 *
 * PROBLEM
 * -------
 * {@link usePermissionDialog} only feeds an in-page overlay
 * (`SecurityDialog.vue`, top-right, `v-if="isVisible && currentRequest"`).
 * When the QAI AppBuilder tab is backgrounded — the common case while the
 * agent works — a `permission_request` frame still arrives on the global
 * `/api/events` stream and is enqueued, but the user sees NOTHING: the
 * overlay is off-screen, and there is no other cue. The backend then blocks
 * on the ASK (or times out to DENY) while the user remains unaware. This is
 * the "no cue ⇒ invisible hang" bug.
 *
 * FIX
 * ---
 * Watch the shared queue count and, whenever it GROWS while the tab is
 * hidden, prefix `document.title` with a bell + count badge
 * (`🔔 (N) <base>`). A background tab's title is still rendered in the
 * Chrome tab strip / window title, so the cue is visible without returning
 * to the tab and WITHOUT any browser permission (unlike the Notification
 * API, which needs a user-gesture-gated grant). The badge is cleared as
 * soon as the tab becomes visible again (the overlay is then on-screen) or
 * the queue empties.
 *
 * The badge is derived by STRIPPING any existing badge before re-rendering,
 * so it never stacks and it survives an external `document.title` write
 * (e.g. the router guard on navigation) without restoring a stale base.
 *
 * Scope: intentionally minimal — title badge only. A follow-up could add a
 * desktop notification (gated behind an explicit settings toggle that
 * requests `Notification` permission on a user gesture) for the
 * fully-minimized-window case.
 */
import { watch, type ComputedRef, type Ref } from "vue";

/** Matches our own badge prefix so re-rendering never stacks and `clear`
 *  can strip it even if an external writer (router guard) replaced the rest
 *  of the title in the meantime. Kept as a constant so render/clear can
 *  never drift apart. */
const BADGE_PREFIX_RE = /^🔔 \(\d+\) /;

/** Handle returned by {@link usePermissionAlert}; call `stop()` on unmount
 *  (or in tests) to detach the listener and drop any active badge. */
export interface PermissionAlertHandle {
  stop: () => void;
}

/**
 * Render the badge over the CURRENT title, preserving whatever base the
 * router guard (or anything else) last wrote. Idempotent: an existing badge
 * is stripped first.
 */
function renderBadge(n: number): void {
  if (typeof document === "undefined") return;
  const base = document.title.replace(BADGE_PREFIX_RE, "");
  document.title = `🔔 (${n}) ${base}`;
}

/** Remove our badge prefix if present, leaving the rest of the title
 *  untouched (a no-op when no badge is active). */
function clearBadge(): void {
  if (typeof document === "undefined") return;
  document.title = document.title.replace(BADGE_PREFIX_RE, "");
}

/**
 * Attach the background-tab permission badge.
 *
 * @param count Reactive pending-authorization count. `App.vue` passes the
 *              shared `usePermissionDialog().queueCount`; a plain `ref` is
 *              accepted so the composable stays unit-testable without the
 *              dialog singleton / Pinia / i18n.
 */
export function usePermissionAlert(
  count: Ref<number> | ComputedRef<number>,
): PermissionAlertHandle {
  if (typeof document === "undefined") {
    return { stop: () => {} };
  }

  function onVisibilityChange(): void {
    if (document.hidden) {
      // A tab hidden WHILE requests are already pending (e.g. the user
      // switches away after the request landed) also needs the cue.
      if (count.value > 0) renderBadge(count.value);
    } else {
      // Overlay is on-screen again — drop the badge.
      clearBadge();
    }
  }

  const stopWatch = watch(count, (now, prev) => {
    if (now === 0) {
      clearBadge();
      return;
    }
    // Only a NEW arrival while hidden needs the cue; while visible the
    // overlay itself is the cue, so do not touch the title.
    if (document.hidden && now > prev) renderBadge(now);
  });

  document.addEventListener("visibilitychange", onVisibilityChange);

  return {
    stop(): void {
      stopWatch();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      clearBadge();
    },
  };
}
