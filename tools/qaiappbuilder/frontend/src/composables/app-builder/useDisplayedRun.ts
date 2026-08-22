// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useDisplayedRun — live "displayed run" projection for the App Builder
 * workbench overlay (cohesion split).
 *
 * Extracted from `AppBuilderWorkbenchOverlay.vue` so the overlay shell
 * stays a thin layout host (its docstring promises "heavy logic lives in
 * composables"). Owns the one tricky concern the shell should not: keeping
 * a local `displayed` ref in sync with the Pinia store's run state across
 * BOTH non-live changes (model selection / history navigation, via watch)
 * AND live SSE mutations (output/logs/status mutate the reactive proxy
 * in-place, which a plain getter would miss — so we poll every 100ms while
 * `store.live`).
 *
 * Returns `displayed` plus the small read-only projections that depend on
 * it (`runStatus` / `isLive` / `runProgress`). No template, no styling —
 * pure reactive state + lifecycle, safe inside the overlay's setup.
 */
import { computed, onUnmounted, ref, watch, type ComputedRef, type Ref } from "vue";
import type { AppRun, useAppBuilderStore } from "@/stores/appBuilder";

type AppBuilderStore = ReturnType<typeof useAppBuilderStore>;

export interface UseDisplayedRunReturn {
  displayed: Ref<AppRun | null>;
  runStatus: ComputedRef<string>;
  isLive: ComputedRef<boolean>;
  runProgress: ComputedRef<{ phase: string; pct: number | null }>;
}

export function useDisplayedRun(store: AppBuilderStore): UseDisplayedRunReturn {
  // `displayed` must reflect live SSE mutations to run.output/logs/status.
  // In a Pinia setup store, `runs.value[idx]` is a raw object while
  // `store.runs[idx]` is the reactive proxy. We poll the reactive proxy
  // every 100 ms while a run is live so the UI stays in sync.
  const displayed = ref<AppRun | null>(null);

  function resolveDisplayed(): AppRun | null {
    const snap = store.snapshotRun;
    if (snap !== null) return snap;
    const idx = store.currentRunIndex;
    return idx >= 0 ? (store.runs[idx] ?? null) : null;
  }

  displayed.value = resolveDisplayed();

  // Reactive watch for non-live changes (model selection, history navigation).
  watch(
    () => [store.currentRunIndex, store.snapshotRun] as const,
    () => {
      displayed.value = resolveDisplayed();
    },
    { immediate: true },
  );

  // Polling for live SSE mutations (output/logs/status change on the proxy).
  let livePoller: ReturnType<typeof setInterval> | null = null;
  watch(
    () => store.live,
    (isLiveNow) => {
      if (isLiveNow) {
        livePoller = setInterval(() => {
          displayed.value = resolveDisplayed();
        }, 100);
      } else {
        if (livePoller !== null) {
          clearInterval(livePoller);
          livePoller = null;
        }
        // Final update after run completes.
        displayed.value = resolveDisplayed();
      }
    },
    { immediate: true },
  );

  onUnmounted(() => {
    if (livePoller !== null) {
      clearInterval(livePoller);
      livePoller = null;
    }
  });

  const runStatus = computed<string>(() => displayed.value?.status ?? "idle");
  const isLive = computed<boolean>(() => store.live);

  // Progress projection (V1 parity, useAppBuilder.js progress frames): the
  // runner emits frames with `payload.phase` + `payload.pct` (0-100); surface
  // the latest so the progress bar reflects real streaming state.
  const runProgress = computed<{ phase: string; pct: number | null }>(() => {
    const run = displayed.value;
    if (run === null || run.frames.length === 0) return { phase: "", pct: null };
    for (let i = run.frames.length - 1; i >= 0; i--) {
      const p = run.frames[i]?.payload as Record<string, unknown> | undefined;
      if (p === undefined) continue;
      const phase = typeof p.phase === "string" ? p.phase : null;
      const pct =
        typeof p.pct === "number"
          ? p.pct
          : typeof p.progress === "number"
            ? p.progress
            : null;
      if (phase !== null || pct !== null) {
        return { phase: phase ?? "", pct };
      }
    }
    return { phase: "", pct: null };
  });

  return { displayed, runStatus, isLive, runProgress };
}
