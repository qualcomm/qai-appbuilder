// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Downloads orchestrator singleton.
 *
 * The Download Center mounts under TWO routes (`/downloads` and `/updates`,
 * mirroring the V1 nav key while preserving the V2-introduced `/downloads`).
 * Both routes share the same in-flight SSE downloads, aria2c poll timer
 * and download settings — so the orchestrator must outlive the panel
 * component (Vue tears it down on route swap).
 *
 * Implementation: a module-scoped lazy singleton created inside a **detached
 * `effectScope`** (`effectScope(true)`). This is the crux of the V1-parity
 * fix: V1's Download Center lives in a `v-show` panel that never unmounts, so
 * its `downloadStatuses`, reactive derivations and aria2c poll timer stay
 * alive while the user visits other tabs and a download keeps streaming. V2
 * navigates by route, so the panel component unmounts on navigate-away.
 *
 * Previously the singleton was created in the *first* component's setup
 * scope, which meant the orchestrator's reactive effects + `onScopeDispose`
 * (e.g. `useAria2c`'s poll timer) were torn down when that component
 * unmounted. The `downloads` ref kept its data and the aria2c daemon kept
 * downloading in the background, but the UI's reactive bindings went stale —
 * on return the card showed "not downloaded" even though the download was
 * still running, and a re-download attempt then collided with the partially
 * written file. Hosting the singleton in a detached scope keeps every effect
 * and timer alive for the whole app session (matching V1), so navigating away
 * and back restores the live download state correctly.
 *
 * We deliberately do NOT wrap it in a pinia `defineStore`, because pinia
 * auto-unwraps refs exposed from the setup function and that breaks the
 * `UseDownloadCenterReturn` typing (the orchestrator's sub-composables expose
 * `Ref<…>` directly so child components can pass them around).
 *
 * Caveats:
 *   - Because the scope is detached, `onMounted` hooks registered inside the
 *     orchestrator (`useAria2c`) never fire. The orchestrator's only mount
 *     side-effect is the initial aria2c `refresh()`, which we invoke
 *     explicitly here after creation.
 *   - `_resetDownloadsStoreSingleton` exists for vitest harness teardown only.
 */

import { effectScope, type EffectScope } from "vue";

import {
  useDownloadCenter,
  type UseDownloadCenterReturn,
} from "@/composables/useDownloadCenter";

let cached: UseDownloadCenterReturn | null = null;
let scope: EffectScope | null = null;

/**
 * Get-or-create the Download Center orchestrator. Safe to call from any
 * context: the orchestrator is created inside a detached `effectScope` so its
 * reactive effects + aria2c poll timer outlive every component that uses it.
 */
export function useDownloadsStore(): UseDownloadCenterReturn {
  if (cached === null) {
    scope = effectScope(true);
    const created = scope.run(() => useDownloadCenter());
    // `scope.run` only returns `undefined` if the scope is already inactive,
    // which cannot happen for a freshly created scope.
    cached = created as UseDownloadCenterReturn;
    // `onMounted` does not fire in a detached scope; trigger the orchestrator's
    // sole mount side-effect (initial aria2c availability probe) explicitly.
    void cached.aria2c.refresh();
  }
  return cached;
}

/** Test-only: drop the singleton + dispose any timers / SSE streams. */
export function _resetDownloadsStoreSingleton(): void {
  if (cached !== null) {
    try {
      cached.dispose();
    } catch {
      /* ignore */
    }
    cached = null;
  }
  if (scope !== null) {
    try {
      scope.stop();
    } catch {
      /* ignore */
    }
    scope = null;
  }
}
