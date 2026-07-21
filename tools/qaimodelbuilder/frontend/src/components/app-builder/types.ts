// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Shared view-model types for the App Builder workbench (chat-mode overlay).
 *
 * The backend `/models` list is intentionally lean; the rich card / drawer
 * fields live in `/models/{id}/manifest`. The overlay merges both into a
 * single normalized {@link AppModelCardVM} so the V1-parity `ModelCard` /
 * `ModelInfoDrawer` can render without caring where each field came from.
 */

/**
 * Per-pack dependency-install progress (V1 `deps-status` row). Held in the
 * store's reactive `depsProgress` map keyed by modelId and merged into the
 * card VM by `buildModelCardVM`. Mirrors V1
 * `useAppBuilderRegistry.js:287-309`.
 */
export interface DepsProgressEntry {
  depsStatus: "ready" | "missing" | "installing" | null;
  depsErrorKind: string | null;
  depsErrorHint: string | null;
  depsErrorRaw: string | null;
  depsMissing: readonly string[];
}

export interface AppModelRuntimeVM {
  backend?: string | null;
  delegate?: string | null;
  quantization?: string | null;
}

export interface AppModelMetricsVM {
  latencyMs?: number | null;
  memoryMB?: number | null;
}

export interface AppModelVariantVM {
  id: string;
  label?: string | null;
  isDefault?: boolean;
  runtime?: AppModelRuntimeVM | null;
  sizeMB?: number | null;
  installed?: boolean;
  /** Registry status (Ready / NotInstalled / ...) — drawer status dot. */
  status?: string | null;
  /** Per-variant latency (ms) for the drawer variant rows. */
  latencyMs?: number | null;
  /** Per-variant install path for the drawer Install Path section. */
  installPath?: string | null;
}

/** One usage example (V1 manifest `examples[]`). */
export interface AppModelExampleVM {
  name?: string | null;
  license?: string | null;
  inputs?: Record<string, unknown>;
  paramsOverride?: Record<string, unknown>;
}

export interface AppModelSchemaKindVM {
  kind?: string | null;
}

/**
 * Card / drawer view-model. `modelId` mirrors V1's manifest field name so the
 * ported ModelCard logic reads naturally; the overlay maps the V2
 * `AppModelResponse.id` into it.
 */
export interface AppModelCardVM {
  modelId: string;
  displayName: string;
  description?: string | null;
  longDescription?: string | null;
  category?: string | null;
  /** registry status: Ready / NotInstalled / Updating / Downloading / Error */
  status?: string | null;
  featured?: boolean;
  runtime?: AppModelRuntimeVM | null;
  metrics?: AppModelMetricsVM | null;
  variants?: AppModelVariantVM[];
  inputSchema?: AppModelSchemaKindVM | null;
  outputSchema?: AppModelSchemaKindVM | null;
  license?: string | null;
  vendor?: string | null;
  version?: string | null;
  /** Manifest top-level tags chips (info drawer). */
  tags?: readonly string[] | null;
  /** Human-readable capability labels (info drawer; internal caps filtered). */
  capabilities?: readonly string[] | null;
  /** Usage examples (info drawer; clickable to apply). */
  examples?: ReadonlyArray<AppModelExampleVM> | null;
  /** Weights download URL (info drawer). */
  weightsUrl?: string | null;
  /** Install path (info drawer; single/legacy pack). */
  installPath?: string | null;
  /** Whether the model is user-imported (enables delete panel in drawer). */
  userImported?: boolean;
  /**
   * Dependency-install progress (V1 parity:
   * `useAppBuilderRegistry.js:287-309`). Merged in-place by the store's
   * `pollDepsStatus` from `GET /api/app-builder/deps-status/packs`. Drives
   * the ModelCard deps badge ("installing → ready / missing") + the install
   * error tooltip. `undefined`/`null` = not yet probed (treat as unknown).
   */
  depsStatus?: "ready" | "missing" | "installing" | null;
  /** Pip-error classification token (V1 `errorKind`), e.g. `tls_cert`. */
  depsErrorKind?: string | null;
  /** Human-readable pip-error hint (V1 `errorHint`) for the tooltip. */
  depsErrorHint?: string | null;
  /** Raw pip stderr tail (V1 `errorRaw`); kept for diagnostics. */
  depsErrorRaw?: string | null;
  /** Missing requirement specifiers (V1 `missing[]`). */
  depsMissing?: readonly string[];
}
