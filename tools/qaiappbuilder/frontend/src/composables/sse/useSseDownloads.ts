// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useSseDownloads — SSE composable for the model_catalog (downloads) context.
 *
 * Per api-contract section 3.2, downloads streams emit:
 *
 *   event: progress      data: { job_id, bytes_downloaded, total_bytes, state, ... }
 *   event: error         data: ApiErrorPayload
 *   event: done          data: { job_id, ... }
 *
 * The composable surfaces a `progress` ref (latest frame) plus an
 * `entries` ref (full per-job rolling state). Consumers usually bind
 * to `entries` for table-like UIs and `progress` for the most-recent
 * toast-style notification.
 *
 * Transport: WebSocket-first with SSE fallback. The WS path is derived
 * from the SSE path by replacing the trailing `/progress` with `/ws`.
 * Callers can override via `opts.wsPath`.
 */
import { ref, type Ref } from "vue";

import { useSse, type UseSseOptions, type UseSseReturn } from "./useSse";

export interface DownloadProgressFrame {
  readonly job_id?: string;
  readonly bytes_downloaded?: number;
  readonly total_bytes?: number;
  readonly state?: string;
  readonly [key: string]: unknown;
}

export interface UseSseDownloadsReturn extends UseSseReturn {
  readonly progress: Ref<DownloadProgressFrame | null>;
  /** Per-job rolling state, keyed by `job_id`. */
  readonly entries: Ref<Readonly<Record<string, DownloadProgressFrame>>>;
  readonly resetEntries: () => void;
}

/** Derive WS path from the SSE progress path. */
function deriveWsPath(ssePath: string | (() => string)): string | (() => string) {
  if (typeof ssePath === "function") {
    return () => {
      const p = ssePath();
      return p.endsWith("/progress") ? p.slice(0, -"/progress".length) + "/ws" : p;
    };
  }
  return ssePath.endsWith("/progress")
    ? ssePath.slice(0, -"/progress".length) + "/ws"
    : ssePath;
}

export function useSseDownloads(
  path: string | (() => string),
  opts: UseSseOptions = {},
): UseSseDownloadsReturn {
  const progress: Ref<DownloadProgressFrame | null> = ref(null);
  const entries: Ref<Record<string, DownloadProgressFrame>> = ref({});

  // Enable WS-first transport: derive wsPath from the SSE path unless
  // the caller explicitly set one.
  const effectiveOpts: UseSseOptions = {
    ...opts,
    wsPath: opts.wsPath ?? deriveWsPath(path),
  };

  const base = useSse(
    path,
    () => ({
      onProgress: (data) => {
        const frame = (data ?? {}) as DownloadProgressFrame;
        progress.value = frame;
        const id = frame.job_id;
        if (typeof id === "string" && id !== "") {
          entries.value = { ...entries.value, [id]: frame };
        }
      },
    }),
    effectiveOpts,
  );

  function resetEntries(): void {
    entries.value = {};
    progress.value = null;
  }

  return {
    state: base.state,
    lastError: base.lastError,
    open: base.open,
    close: base.close,
    progress,
    entries,
    resetEntries,
  };
}
