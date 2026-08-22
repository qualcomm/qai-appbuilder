// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useHeaderActions(factory)` — register topbar right-side buttons from
 * within a view component.
 *
 * Why a composable + factory
 * --------------------------
 * The factory closure is wrapped in `watchEffect` so the actions array
 * is recomputed whenever any reactive dependency it touches changes
 * (e.g. an `isLoading` ref, a `disabled` computed, a translated label).
 * This avoids the boilerplate of manually `watch()`ing every dependency
 * inside the view.
 *
 * Lifecycle (KeepAlive-aware)
 * ---------------------------
 *  - Topbar actions are owned by exactly one visible view at a time. Since
 *    `AppMain.vue` wraps `<RouterView>` in `<KeepAlive>`, navigating away
 *    from a view does NOT trigger `onBeforeUnmount` — it triggers
 *    `onDeactivated`. If this composable only listened to `onBeforeUnmount`
 *    (the pre-KeepAlive design), every cached view would keep its
 *    `watchEffect` alive and keep racing to overwrite the topbar buttons,
 *    leaving stale Service buttons visible in Channels, etc.
 *  - We therefore mirror onMounted/onBeforeUnmount with onActivated/
 *    onDeactivated: starting watchEffect (and registering actions) on
 *    activate, stopping the watcher AND clearing the store on deactivate /
 *    unmount. Whichever pair fires (real mount lifecycle for non-cached
 *    routes, activate/deactivate for KeepAlive-cached ones) is sufficient
 *    on its own; the duplicate hooks are no-ops thanks to the `stop` flag.
 *  - The store holds at most one view's actions at a time — this is by
 *    design. Two views cannot both be "active" in the user's view at once
 *    (KeepAlive deactivates the previously-shown route before activating
 *    the new one), so a single-slot registry is sufficient.
 *
 * V1 parity reference: index.html:316-397 — V1 uses
 * `v-if="currentView === '…'"` blocks inline in the global topbar to
 * the same effect; this composable expresses that pattern in V2's
 * component-isolation model without breaking AppHeader / RouterView
 * sibling boundaries (see `stores/headerActions.ts` rationale).
 */
import {
  onActivated,
  onBeforeUnmount,
  onDeactivated,
  onMounted,
  watchEffect,
  type WatchStopHandle,
} from "vue";
import {
  useHeaderActionsStore,
  type HeaderAction,
} from "@/stores/headerActions";

/**
 * Register a reactive list of topbar actions for the current view.
 *
 * The factory is re-run automatically whenever any reactive value it
 * reads changes (so it should be a thin closure that returns a fresh
 * array each call).
 *
 * @example
 * useHeaderActions(() => [
 *   {
 *     id: "settings.refresh",
 *     label: t("common.refresh"),
 *     icon: "🔄",
 *     disabled: refreshing.value,
 *     onClick: handleRefresh,
 *   },
 * ]);
 */
export function useHeaderActions(factory: () => HeaderAction[]): void {
  const store = useHeaderActionsStore();
  let stop: WatchStopHandle | null = null;

  function start(): void {
    if (stop !== null) return; // already active
    stop = watchEffect(() => {
      store.set(factory());
    });
  }

  function teardown(): void {
    if (stop !== null) {
      stop();
      stop = null;
    }
    store.clear();
  }

  // Non-KeepAlive path: regular mount/unmount.
  onMounted(start);
  onBeforeUnmount(teardown);
  // KeepAlive path: activate/deactivate on every show/hide. onActivated also
  // fires right after the first onMounted, but `start()` is idempotent.
  onActivated(start);
  onDeactivated(teardown);
}
