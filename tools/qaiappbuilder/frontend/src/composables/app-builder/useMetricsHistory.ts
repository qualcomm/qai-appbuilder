// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useMetricsHistory — derives a latency history projection from the App Builder
 * run list (V1 `MetricsView.js` parity for the headline + history sparkline +
 * p50/p95/max stats; V2 currently exposes only `duration_ms` per run via
 * `RunMetricsResponse`, so that is the single dimension we project).
 *
 * Pure / side-effect-free: takes a reactive `runs` source + an optional limit
 * and returns memoized projections. Lives outside the component so the view
 * stays a thin renderer (Clean Arch / single-responsibility) and so the
 * percentile / formatting helpers can be unit-tested independently.
 */
import { computed, type ComputedRef, type MaybeRefOrGetter, toValue } from "vue";

import type { AppRun } from "@/stores/appBuilder";

/** Default ring-buffer size for the sparkline (V1: most recent N runs). */
export const DEFAULT_HISTORY_LIMIT = 20;

/** Result shape consumed by `MetricsView.vue`. */
export interface MetricsHistory {
  /** Latency samples (ms), oldest → newest, ready for the sparkline path. */
  points: ComputedRef<number[]>;
  /** Sample count actually projected (≤ limit). */
  count: ComputedRef<number>;
  /** p50 latency in ms (or `null` when fewer than 1 sample). */
  p50: ComputedRef<number | null>;
  /** p95 latency in ms (or `null` when fewer than 1 sample). */
  p95: ComputedRef<number | null>;
  /** max latency in ms (or `null` when fewer than 1 sample). */
  max: ComputedRef<number | null>;
  /** True when the projection has too few points to draw a meaningful trend. */
  indeterminate: ComputedRef<boolean>;
}

// ── Helpers (exported for unit tests / view-side reuse) ─────────────────────

/** Format a millisecond duration as "320 ms" / "1.15 s". */
export function formatMs(value: number | null | undefined): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n >= 1000) return `${(n / 1000).toFixed(2)} s`;
  return `${Math.round(n)} ms`;
}

/** Format a megabyte count as "128 MB" / "1.25 GB". */
export function formatMB(value: number | null | undefined): string {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return "—";
  if (n >= 1024) return `${(n / 1024).toFixed(2)} GB`;
  return `${Math.round(n)} MB`;
}

/**
 * Linear-interpolation percentile over a finite sample (ms). Returns `null`
 * for empty input. `q` is the quantile in [0, 1] (e.g. 0.5 → median).
 */
export function percentile(samples: readonly number[], q: number): number | null {
  if (samples.length === 0) return null;
  if (samples.length === 1) return samples[0] ?? null;
  const sorted = [...samples].sort((a, b) => a - b);
  const clamped = Math.min(1, Math.max(0, q));
  const idx = clamped * (sorted.length - 1);
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return sorted[lo] ?? null;
  const loVal = sorted[lo] ?? 0;
  const hiVal = sorted[hi] ?? 0;
  return loVal + (hiVal - loVal) * (idx - lo);
}

// ── Composable ──────────────────────────────────────────────────────────────

/**
 * Project a latency history (oldest → newest) + p50/p95/max from the App
 * Builder `runs` array. `runs` is the V2 store ordering (newest first, V1
 * parity), so we reverse + take the trailing `limit` to render left-to-right.
 *
 * Only `duration_ms` is used (V2 `RunMetricsResponse` has no per-stage / p50
 * payload yet); when that field is missing the run is skipped, matching V1's
 * "successful run" filter.
 */
export function useMetricsHistory(
  runsSource: MaybeRefOrGetter<readonly AppRun[] | null | undefined>,
  limit: MaybeRefOrGetter<number> = DEFAULT_HISTORY_LIMIT,
): MetricsHistory {
  const points = computed<number[]>(() => {
    const runs = toValue(runsSource);
    if (!Array.isArray(runs) || runs.length === 0) return [];
    const max = Math.max(1, Math.floor(toValue(limit) ?? DEFAULT_HISTORY_LIMIT));
    const samples: number[] = [];
    // `runs` is newest-first → walk backwards so the result is chronological.
    for (let i = runs.length - 1; i >= 0; i -= 1) {
      const run = runs[i];
      if (run === undefined || run === null) continue;
      const ms = run.metrics?.duration_ms;
      if (typeof ms !== "number" || !Number.isFinite(ms)) continue;
      samples.push(ms);
      if (samples.length >= max) break;
    }
    return samples;
  });

  const count = computed<number>(() => points.value.length);

  const p50 = computed<number | null>(() => percentile(points.value, 0.5));
  const p95 = computed<number | null>(() => percentile(points.value, 0.95));
  const max = computed<number | null>(() => {
    const arr = points.value;
    if (arr.length === 0) return null;
    let m = -Infinity;
    for (const v of arr) if (v > m) m = v;
    return Number.isFinite(m) ? m : null;
  });

  const indeterminate = computed<boolean>(() => points.value.length < 2);

  return { points, count, p50, p95, max, indeterminate };
}
