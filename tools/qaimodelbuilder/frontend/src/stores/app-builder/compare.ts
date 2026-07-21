// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * App Builder compare-tray pure builders + URL helper.
 *
 * Extracted verbatim from `stores/appBuilder.ts` (ARCH-1 cohesion split).
 * `buildCompareItem` enriches a run into a compare-tray view-model from the
 * model row + manifest; `artifactBlobUrl` builds a blob URL for an artifact
 * path. Both are pure (no reactive state) and called by the store actions.
 */
import { buildApiUrl } from "@/api";
import type { AppModelResponse, AppRun, CompareItem } from "./types";

/**
 * Enrich a run into a compare-tray item (V1 CompareItem carries modelName /
 * runtime / status / rating so the Table & Radar views have real columns).
 * Pure: caller resolves `model` from the registry and `manifest` from cache.
 */
export function buildCompareItem(
  run: AppRun,
  model: AppModelResponse | undefined,
  manifest: Record<string, unknown> | undefined,
): CompareItem {
  const rt = (manifest?.runtime ?? null) as Record<string, unknown> | null;
  const mt = (manifest?.metrics ?? null) as Record<string, unknown> | null;
  return {
    id: run.id as string,
    modelId: run.modelId,
    modelName: model?.title ?? run.modelId,
    status: run.status,
    rating: 0,
    variant: run.variantId,
    runtime:
      rt !== null
        ? {
            backend: typeof rt.backend === "string" ? rt.backend : null,
            quantization:
              typeof rt.quantization === "string" ? rt.quantization : null,
          }
        : null,
    output: run.output,
    metrics: {
      latencyMs: run.metrics?.duration_ms ?? null,
      memoryMB: typeof mt?.memoryMB === "number" ? mt.memoryMB : null,
      modelSizeMB: typeof rt?.modelSizeMB === "number" ? rt.modelSizeMB : null,
    },
  };
}

/** Build a blob URL for an artifact path (G6). */
export function artifactBlobUrl(runId: string, relativePath: string): string {
  const clean = relativePath
    .split("/")
    .map((seg) => encodeURIComponent(seg))
    .join("/");
  return buildApiUrl(
    `/api/app-builder/artifacts/${encodeURIComponent(runId)}/${clean}/blob`,
    undefined,
  );
}
