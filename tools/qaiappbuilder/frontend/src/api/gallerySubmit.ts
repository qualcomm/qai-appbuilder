// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Gallery submission API client (Model Builder → Model Gallery promotion).
 *
 * Covers the gallery upload workflow:
 *   GET  /api/gallery/scan   — enumerate convertible artifacts in workdir
 *   POST /api/gallery/submit — package & upload to the Model Gallery dashboard
 *
 * Wire field names mirror `interfaces/http/routes/gallery_submit.py`
 * (snake_case). These wrap the typed client so the useGallerySubmit
 * composable stays transport-agnostic.
 */

import { apiJson, type ApiRequestOptions } from "./http";

// ── DTOs ───────────────────────────────────────────────────────────────────

/** One file entry returned by the scan endpoint. */
export interface GalleryFileInfo {
  readonly path: string;
  readonly size: number;
  readonly selected: boolean;
  readonly type: string;
  readonly filename: string;
  /** Path relative to the scanned workdir (POSIX). Equals filename for top-level files. */
  readonly rel_path?: string;
}

/** Response shape from `GET /api/gallery/scan`. */
export interface GalleryScanResponseDTO {
  readonly model_name: string;
  readonly category: "genai" | "non_genai";
  readonly qnn_version: string;
  readonly quant_method: string;
  readonly files: GalleryFileInfo[];
}

/** Request body for `POST /api/gallery/submit`. */
export interface GallerySubmitRequestDTO {
  readonly submitter: string;
  readonly email: string;
  readonly model_name: string;
  readonly model_category: "genai" | "non_genai";
  readonly model_type?: string;
  readonly customer?: string;
  readonly qnn_version?: string;
  readonly quant_method?: string;
  readonly scenario?: string;
  readonly notebook_url?: string;
  readonly description?: string;
  readonly performance?: string;
  readonly file_paths: string[];
}

/** Response shape from `POST /api/gallery/submit`. */
export interface GallerySubmitResponseDTO {
  readonly upload_id: string;
  readonly status_url: string;
  readonly success: boolean;
  readonly message: string;
}

// ── API functions ──────────────────────────────────────────────────────────

/** Scan a workdir for gallery-submittable artifacts. */
export async function scanGalleryFiles(
  workdir: string,
  opts?: ApiRequestOptions,
): Promise<GalleryScanResponseDTO> {
  return apiJson<GalleryScanResponseDTO>("GET", "/api/gallery/scan", undefined, {
    ...opts,
    query: { workdir },
  });
}

/** Immediate response from `POST /api/gallery/submit` (upload runs in background). */
export interface GallerySubmitAcceptedDTO {
  readonly upload_id: string;
  readonly status: string;
}

/** Progress snapshot from `GET /api/gallery/submit-progress/{upload_id}`. */
export interface GallerySubmitProgressDTO {
  readonly status: "packaging" | "uploading" | "done" | "error";
  readonly phase: "packaging" | "uploading" | "done" | "error";
  readonly current_file: string | null;
  readonly files_done: number;
  readonly files_total: number;
  readonly result: GallerySubmitResponseDTO | null;
  readonly error_code: string | null;
  readonly error_message: string | null;
}

/** Launch a background upload to the Model Gallery dashboard. */
export async function submitToGallery(
  body: GallerySubmitRequestDTO,
  opts?: ApiRequestOptions,
): Promise<GallerySubmitAcceptedDTO> {
  return apiJson<GallerySubmitAcceptedDTO>("POST", "/api/gallery/submit", body, opts);
}

/** Poll the byte-level progress of a background gallery upload. */
export async function getGallerySubmitProgress(
  uploadId: string,
  opts?: ApiRequestOptions,
): Promise<GallerySubmitProgressDTO> {
  return apiJson<GallerySubmitProgressDTO>(
    "GET",
    `/api/gallery/submit-progress/${encodeURIComponent(uploadId)}`,
    undefined,
    opts,
  );
}

/** Open a file with the system default application. */
export async function openFileInSystem(
  filePath: string,
  opts?: ApiRequestOptions,
): Promise<{ success: boolean }> {
  return apiJson<{ success: boolean }>("POST", "/api/gallery/open-file", { path: filePath }, opts);
}

/** Permanently delete a file from the workspace filesystem. */
export async function deleteFileFromSystem(
  filePath: string,
  opts?: ApiRequestOptions,
): Promise<{ success: boolean }> {
  return apiJson<{ success: boolean }>("POST", "/api/gallery/delete-file", { path: filePath }, opts);
}

/** Generate a gallery description by summarising workspace docs via the cloud model. */
export async function generateGalleryDescription(
  workdir: string,
  modelId?: string,
  opts?: ApiRequestOptions,
): Promise<{ description: string }> {
  return apiJson<{ description: string }>(
    "POST",
    "/api/gallery/generate-description",
    { workdir, model_id: modelId },
    opts,
  );
}
