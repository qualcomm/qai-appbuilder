// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * App Builder run-history pure transforms.
 *
 * Extracted verbatim from `stores/appBuilder.ts` (ARCH-1 cohesion split).
 * `hydrateHistoryRun` maps a server `RunResponse` to the local `AppRun`
 * view-model; `mergeHistoryRuns` merges freshly-hydrated history rows with
 * the in-memory runs, preserving SSE-streamed fields the history API never
 * returns. Both are pure (no reactive state) and called by the store's
 * `fetchHistory` action.
 */
import { mapBackendRating } from "./frames";
import type { AppRun, RunResponse } from "./types";

/** Hydrate one server `RunResponse` into the local `AppRun` view-model. */
export function hydrateHistoryRun(r: RunResponse): AppRun {
  return {
    id: r.id,
    modelId: r.model_id,
    variantId:
      typeof r.inputs?.variant_id === "string"
        ? (r.inputs.variant_id as string)
        : null,
    status: r.status,
    inputs: r.inputs ?? {},
    params:
      r.inputs !== undefined &&
      r.inputs !== null &&
      typeof r.inputs.params === "object" &&
      r.inputs.params !== null
        ? (r.inputs.params as Record<string, unknown>)
        : {},
    frames: [],
    output: null,
    logs: [],
    metrics: null,
    artifacts: r.artifacts ?? [],
    error: r.error_message ?? null,
    createdAt: Date.parse(r.created_at) || Date.now(),
    startedAt: r.started_at ?? null,
    finishedAt: r.finished_at ?? null,
    pid: null,
    exitCode: null,
    statusHint: null,
    queuePosition: null,
    errorDetail: null,
    rating: mapBackendRating(r.rating),
  };
}

/**
 * Merge freshly-hydrated history rows with the in-memory runs.
 *
 * For runs already in memory (matched by id), preserve SSE-streamed fields
 * (frames / output / logs / pid / exitCode / statusHint) that the history API
 * never returns, and only update the persistent fields that the server owns
 * (status / metrics / artifacts / error / startedAt / finishedAt).
 */
export function mergeHistoryRuns(
  hydrated: AppRun[],
  existingById: Map<string | null, AppRun>,
): AppRun[] {
  return hydrated.map((histRun) => {
    const existing = existingById.get(histRun.id);
    if (existing) {
      // Preserve in-memory SSE data; only overwrite server-owned fields.
      return {
        ...existing,
        status: histRun.status,
        metrics: histRun.metrics ?? existing.metrics,
        artifacts:
          histRun.artifacts.length > 0 ? histRun.artifacts : existing.artifacts,
        error: histRun.error ?? existing.error,
        startedAt: histRun.startedAt ?? existing.startedAt,
        finishedAt: histRun.finishedAt ?? existing.finishedAt,
      };
    }
    return histRun;
  });
}
