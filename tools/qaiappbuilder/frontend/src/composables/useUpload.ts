// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useUpload` — composable wrapper around `apiUpload` for multipart UI.
 *
 * S7.5 L8 PR-804.
 *
 * Six legacy upload sites need to call into the QAI multipart endpoints:
 *
 *   1. chat image upload          POST /api/images/upload
 *   2. app_builder model upload   POST /api/uploads/model
 *   3. app_builder dataset upload POST /api/uploads/dataset
 *   4. ai_coding code upload      POST /api/uploads/code
 *   5. voice upload               POST /api/uploads/voice
 *   6. dataset upload (catalog)   POST /api/uploads/dataset
 *
 * The L7 lane wires this composable into each SFC; L8 ships the
 * primitive. The composable handles:
 *
 *   - File -> FormData wrapping (with caller-controlled field names
 *     and extra fields)
 *   - Aborting an in-flight upload via AbortController
 *   - Surfacing busy / progress / error state for templates
 *
 * Browser fetch does not currently report upload progress for streaming
 * bodies, so `progress` stays at `null` until the request completes.
 * (XHR-based progress will land as a follow-up if a UX gap appears.)
 */
import { ref, type Ref } from "vue";

import { apiUpload } from "@/api/http";
import type { ApiError } from "@/api/errors";

export interface UseUploadOptions {
  /** Form-data field name for the uploaded file. Defaults to `"file"`. */
  readonly fieldName?: string;
  /** Extra form-data fields appended verbatim. */
  readonly extraFields?: Readonly<Record<string, string>>;
  /** Optional override for the request `credentials` flag. */
  readonly credentials?: "omit" | "same-origin" | "include";
}

export type UploadState = "idle" | "uploading" | "done" | "failed";

export interface UseUploadReturn<TRes = unknown> {
  readonly state: Ref<UploadState>;
  readonly lastError: Ref<ApiError | null>;
  readonly lastResponse: Ref<TRes | null>;
  /**
   * Send a single file (or `Blob`). Returns the parsed response, or
   * throws the `ApiError` for callers that prefer try/catch. Idempotent
   * across reuse — a second call with state `"uploading"` aborts the
   * first and starts a new upload.
   */
  readonly upload: (
    file: File | Blob,
    overrideFieldName?: string,
  ) => Promise<TRes>;
  /** Abort the in-flight upload (or no-op). */
  readonly abort: () => void;
}

export function useUpload<TRes = unknown>(
  path: string | (() => string),
  opts: UseUploadOptions = {},
): UseUploadReturn<TRes> {
  const state: Ref<UploadState> = ref("idle");
  const lastError: Ref<ApiError | null> = ref(null);
  const lastResponse: Ref<TRes | null> = ref(null);

  let controller: AbortController | null = null;

  function abort(): void {
    if (controller !== null) {
      controller.abort();
      controller = null;
      if (state.value === "uploading") {
        state.value = "idle";
      }
    }
  }

  async function upload(
    file: File | Blob,
    overrideFieldName?: string,
  ): Promise<TRes> {
    abort(); // tear down any prior in-flight upload
    controller = new AbortController();
    state.value = "uploading";
    lastError.value = null;

    const fd = new FormData();
    const field = overrideFieldName ?? opts.fieldName ?? "file";
    if (file instanceof File) {
      fd.append(field, file, file.name);
    } else {
      fd.append(field, file);
    }
    if (opts.extraFields !== undefined) {
      for (const [k, v] of Object.entries(opts.extraFields)) {
        fd.append(k, v);
      }
    }

    try {
      const res = await apiUpload<TRes>(
        typeof path === "function" ? path() : path,
        fd,
        {
          signal: controller.signal,
          credentials: opts.credentials,
        },
      );
      lastResponse.value = res;
      state.value = "done";
      return res;
    } catch (cause) {
      lastError.value = cause as ApiError;
      state.value = "failed";
      throw cause;
    } finally {
      controller = null;
    }
  }

  return {
    state,
    lastError,
    lastResponse,
    upload,
    abort,
  };
}
