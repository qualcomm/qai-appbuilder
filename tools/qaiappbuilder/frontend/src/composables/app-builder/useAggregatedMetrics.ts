// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useAggregatedMetrics — fetch the App Builder server-side aggregated metrics
 * for a given (modelId, variantId?) pair (V1 `MetricsView.js` L255-300 parity:
 * the V1 endpoint was `GET /api/appbuilder/metrics/{model_id}?variant_id=...`;
 * the V2 clean-arch route is `GET /api/app-builder/metrics/model/{model_id}`
 * — mounted on a distinct `/metrics/model/...` path so the `{model_id}`
 * parameter never collides with the single-run `/metrics/{run_id}` route).
 * It returns camelCase `{ modelId, variantId, count,
 * latencyMs:{p50,p90,p99,mean,max}|null, memoryMB:{mean,max}|null,
 * rating:{thumbsUp,thumbsDown,qualityScore,count,avg}|null }`.
 *
 * V2 backend status (3-M1 — implemented):
 *   - The endpoint `GET /api/app-builder/metrics/model/{model_id}` is backed
 *     by `GetAggregatedMetricsForModelUseCase` (latency percentiles from each
 *     completed run's wall-clock duration + a folded Likert-rating summary).
 *     An empty history returns HTTP 200 with `count == 0` and `null`
 *     sub-objects (the view hides the panel), so the 404 `pendingBackend`
 *     branch below only fires if the route is ever genuinely absent.
 *   - `memoryMB` is `null` until the runner surfaces a peak-memory metric
 *     (the V2 run aggregate does not capture it yet); the view hides the
 *     memory row in that case.
 *
 * Architecture (judgement 1 / 重构质量铁律 需求 A):
 *   - Pure composable, no Vue component coupling — easy to unit-test.
 *   - Manual fetch trigger (`fetch(modelId, variantId)`) + `refresh()` for the
 *     Refresh button; callers `watch(...)` themselves to decide when to call
 *     (rating change / model switch). No hidden side effects.
 *   - Uses the project-standard `apiJson` (CSRF / base-url / error parsing
 *     centralised), not raw `fetch` — V1 used `window.fetch` directly which
 *     bypassed every shared concern.
 */
import { ref, type Ref } from "vue";

import { apiJson } from "@/api/http";
import { ApiError } from "@/api/errors";

/** V1-compatible aggregated metrics payload (camelCase from server). */
export interface AggregatedMetrics {
  modelId: string;
  variantId: string | null;
  count: number;
  latencyMs: {
    p50?: number | null;
    p90?: number | null;
    p99?: number | null;
    mean?: number | null;
    max?: number | null;
  } | null;
  memoryMB: {
    mean?: number | null;
    max?: number | null;
  } | null;
  rating: {
    thumbsUp: number;
    thumbsDown: number;
    qualityScore: number;
    count: number;
    avg?: number | null;
  } | null;
}

/** Composable return shape. */
export interface UseAggregatedMetrics {
  data: Ref<AggregatedMetrics | null>;
  loading: Ref<boolean>;
  /**
   * `null` when no error, `"pendingBackend"` when the endpoint 404s (route
   * not deployed yet — distinct UI message), or a free-form string for any
   * other failure.
   */
  error: Ref<string | null>;
  /** Fetch latest aggregate for `(modelId, variantId)`. */
  fetch: (modelId: string | null | undefined, variantId?: string | null) => Promise<void>;
  /** Re-fetch using the last `(modelId, variantId)` arguments (no-op until first call). */
  refresh: () => Promise<void>;
  /** Reset state (data + error) — used when the model is unselected. */
  reset: () => void;
}

export function useAggregatedMetrics(): UseAggregatedMetrics {
  const data = ref<AggregatedMetrics | null>(null);
  const loading = ref<boolean>(false);
  const error = ref<string | null>(null);
  let lastArgs: { modelId: string; variantId: string | null } | null = null;

  function reset(): void {
    data.value = null;
    error.value = null;
    lastArgs = null;
  }

  async function fetchInternal(modelId: string, variantId: string | null): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const path = `/api/app-builder/metrics/model/${encodeURIComponent(modelId)}`;
      const result = await apiJson<AggregatedMetrics>("GET", path, undefined, {
        query: variantId !== null ? { variant_id: variantId } : undefined,
      });
      data.value = result ?? null;
    } catch (cause) {
      // 404 = endpoint not implemented yet → mark as pending-backend so the
      // view shows a friendly placeholder instead of an error toast. Other
      // failures (5xx / network) keep their message — UI shows generic
      // "Loading..." → falls back to hidden once error is set.
      if (cause instanceof ApiError && cause.status === 404) {
        error.value = "pendingBackend";
      } else if (cause instanceof Error) {
        error.value = cause.message;
      } else {
        error.value = String(cause);
      }
      data.value = null;
    } finally {
      loading.value = false;
    }
  }

  async function fetch(
    modelId: string | null | undefined,
    variantId: string | null = null,
  ): Promise<void> {
    if (modelId === null || modelId === undefined || modelId === "") {
      reset();
      return;
    }
    lastArgs = { modelId, variantId };
    await fetchInternal(modelId, variantId);
  }

  async function refresh(): Promise<void> {
    if (lastArgs === null) return;
    await fetchInternal(lastArgs.modelId, lastArgs.variantId);
  }

  return { data, loading, error, fetch, refresh, reset };
}
