// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * App Builder import API client (Model Builder → App Builder promotion).
 *
 * Covers the import workflow surfaced by the `app_builder` context:
 *   POST /api/app-builder/import/scan-bins   — enumerate output/*.bin variants
 *   POST /api/app-builder/import/auto-export — one-click Pack generation
 *   POST /api/app-builder/import/dry-run     — validate candidate(s)
 *   POST /api/app-builder/import/commit      — finalize an import plan
 *   POST /api/app-builder/import/rollback    — undo a previous commit
 *
 * Wire field names mirror `interfaces/http/routes/app_builder.py`
 * (snake_case). These wrap the typed client so the PromoteToAppBuilder
 * composable stays transport-agnostic.
 */

import { apiJson, type ApiRequestOptions } from "./http";

// ── scan-bins ──────────────────────────────────────────────────────────────

/** One precision-artifact row from `POST /import/scan-bins`. */
export interface BinScanResultDTO {
  readonly path: string;
  readonly size_bytes: number;
  readonly suspected_model_id?: string | null;
  /** Plan-form precision token (`fp16` / `w8a8` / `w4a16` …). */
  readonly precision?: string | null;
  /** UI display label (`FP16` / `INT8` …). */
  readonly label?: string | null;
  /** ISO-8601 UTC modification timestamp. */
  readonly mtime?: string | null;
}

/** Set when the scan found no `output/` variants but detected a
 *  downloaded-but-not-normalized AI Hub model in the workdir (weight +
 *  metadata.json, often in a nested subfolder). Lets the UI guide the user to
 *  run Step 6.5 normalization instead of showing a blank Import panel. */
export interface NeedsNormalizeDTO {
  readonly model_workdir: string;
  readonly detected_weight: string;
}

export interface BinScanResponseDTO {
  readonly results: BinScanResultDTO[];
  /** Optional (tail-appended): present only when `results` is empty AND an
   *  un-normalized AI Hub package was detected. */
  readonly needs_normalize?: NeedsNormalizeDTO | null;
}

/**
 * Scan `<modelWorkdir>/output/<model>_<label>.bin` for precision
 * variants. When `modelWorkdir` is omitted the backend falls back to a
 * fingerprint-free `scan_root` listing.
 */
export async function scanBins(
  modelWorkdir: string,
  opts?: ApiRequestOptions,
): Promise<BinScanResponseDTO> {
  return apiJson<BinScanResponseDTO>(
    "POST",
    "/api/app-builder/import/scan-bins",
    { model_workdir: modelWorkdir },
    opts,
  );
}

// ── auto-export ──────────────────────────────────────────────────────────────

export interface AutoExportRequestDTO {
  /** Required absolute path to the Model Builder workspace directory. */
  readonly source_path: string;
  readonly model_name?: string;
  /** Plan-form / label-form precision tokens to bundle as variants. */
  readonly precisions?: string[];
  /** Explicit default for the multi-variant case (∈ precisions). */
  readonly default_precision?: string;
}

export interface AutoExportResponseDTO {
  readonly accepted: boolean;
  readonly note: string;
  readonly success: boolean;
  readonly pack_id: string;
  readonly display_name: string;
  readonly source_workdir: string;
  readonly output: string;
  readonly errors: string[];
}

/** One-click Pack generation from a model workspace. */
export async function autoExport(
  body: AutoExportRequestDTO,
  opts?: ApiRequestOptions,
): Promise<AutoExportResponseDTO> {
  return apiJson<AutoExportResponseDTO>(
    "POST",
    "/api/app-builder/import/auto-export",
    body,
    opts,
  );
}

// ── dry-run / commit / rollback ──────────────────────────────────────────────

export interface ImportPlanItemDTO {
  readonly model_id: string;
  readonly action: string;
  readonly source: string;
  readonly reason?: string | null;
  /** V1 parity: human-readable model name (manifest `displayName`).
   *  Renders as the rich card's big title; falls back to `model_id`. */
  readonly display_name?: string | null;
  /** V1 parity: ISO-8601 generation timestamp. Renders in the meta row. */
  readonly generated_at?: string | null;
  /** V1 dry_run parity: hard validation errors (✗) that block import —
   *  missing/too-small weights, runner.py absent/won't compile, missing
   *  required manifest fields. Non-empty ⇒ the candidate is not importable. */
  readonly errors?: readonly string[];
  /** V1 dry_run parity: conflict notes (⚠) — the target id already exists. */
  readonly conflicts?: readonly string[];
  /** V1 dry_run parity: suggested next semver under `bump`. */
  readonly suggested_version?: string | null;
  /** Conflict resolution policy (`bump` / `replace` / `cancel`) sent back on
   *  commit so the importer bumps the version / replaces-with-backup / aborts. */
  readonly conflict_policy?: string;
}

export interface ImportPlanResponseDTO {
  readonly items: ImportPlanItemDTO[];
  readonly is_empty: boolean;
  readonly is_noop: boolean;
}

export interface ImportCommitResponseDTO {
  readonly commit_id: string;
}

export interface ImportRollbackResponseDTO {
  readonly commit_id: string;
  readonly status: string;
}

/** Validate candidate source workdir(s) without mutating anything. */
export async function importDryRun(
  candidates: string[],
  opts?: ApiRequestOptions,
): Promise<ImportPlanResponseDTO> {
  return apiJson<ImportPlanResponseDTO>(
    "POST",
    "/api/app-builder/import/dry-run",
    { candidates },
    opts,
  );
}

/** Execute a previously-validated import plan; returns a commit id.
 *
 * `conflictPolicy` (`bump` / `replace` / `cancel`) is stamped onto every
 * item so the backend importer can bump the version / replace-with-backup /
 * abort per the user's choice (V1 parity — V1 sent `conflictPolicy` on
 * commit; V2 previously dropped it, leaving the policy dropdown a dead
 * control). */
export async function importCommit(
  items: ImportPlanItemDTO[],
  conflictPolicy: string,
  opts?: ApiRequestOptions,
): Promise<ImportCommitResponseDTO> {
  const stamped = items.map((it) => ({
    ...it,
    conflict_policy: conflictPolicy,
  }));
  return apiJson<ImportCommitResponseDTO>(
    "POST",
    "/api/app-builder/import/commit",
    { items: stamped },
    opts,
  );
}

/** Undo a previous commit. Idempotent. */
export async function importRollback(
  commitId: string,
  opts?: ApiRequestOptions,
): Promise<ImportRollbackResponseDTO> {
  return apiJson<ImportRollbackResponseDTO>(
    "POST",
    "/api/app-builder/import/rollback",
    { commit_id: commitId },
    opts,
  );
}
