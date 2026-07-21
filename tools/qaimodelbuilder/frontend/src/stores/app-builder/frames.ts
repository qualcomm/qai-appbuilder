// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * App Builder run-frame demuxing + rating mapping.
 *
 * Extracted verbatim from `stores/appBuilder.ts` (ARCH-1 cohesion split).
 * These are pure functions with no reactive/Pinia dependency: `applyFrameToRun`
 * mutates an `AppRun` object passed in by the caller (the store), and
 * `mapBackendRating` is a pure numeric mapping. Keeping them here keeps the
 * store focused on state + actions wiring.
 */
import type { AppRun } from "./types";

/**
 * Map the backend Likert rating (1..5) returned on `RunResponse.rating`
 * back to the store's internal V1 thumb semantic (-1 / 0 / 1) used by
 * `AppRun.rating` and `ratingEmoji`.
 *
 * Closure with `submitRating` (which POSTs 1→5, -1→1): a reload must
 * surface the same emoji the optimistic update showed. So 5→1 (👍),
 * 1→-1 (👎); any middle value (2/3/4 — only reachable via legacy /
 * non-thumb feedback) collapses to 0 (🙂). `null`/absent stays unrated.
 */
export function mapBackendRating(
  rating: number | null | undefined,
): number | null {
  if (typeof rating !== "number") return null;
  if (rating >= 5) return 1;
  if (rating <= 1) return -1;
  return 0;
}

/**
 * Demux one SSE `frame` payload into the appropriate `AppRun` slot.
 *
 * V2 backend (`apps/.../app_builder/infrastructure/process_runner.py
 * :_decode_stdout_line`) tags every NDJSON v3.1 line with an `event`
 * discriminator: `status | progress | metrics | log | result | done |
 * error | started | terminated | unknown_runner`. V1 parity
 * (`useAppBuilder.js:_handleEvent`):
 *
 *   - `result`  → assemble `run.output` from `payload.output` (the real
 *                 inference result dict — `lines / fullText / image_path /
 *                 audio_path / segments / detections / predictions / …`).
 *                 Multiple `result` frames merge (V1 NDJSON v3.1 contract:
 *                 a Pack may emit several partial results before `done`).
 *   - `log`     → append `{stream, line}` to `run.logs` (Logs Tab).
 *                 Stderr `process_runner` chunks carry `data` instead of
 *                 `line`; both are normalized.
 *   - `started` → record `run.pid` (process id of the worker).
 *   - `terminated` → record `run.exitCode` (exit code; `done`/`error`
 *                    drives `run.status` via `onState` / `onDone`).
 *   - `status`  → record optional `run.statusHint` (V1 statusHint, e.g.
 *                 `model_loaded` / `model_cached`); the actual run status
 *                 comes from the `event:state` SSE channel via `onState`.
 *   - `progress / metrics / done / error / unknown_runner` → no output
 *     mutation; `run.frames` already records the raw payload for the
 *     overlay's progress projection / live history.
 *
 * Backwards-compat: when the payload has NO `event` discriminator (older
 * tests / synthetic fixtures), fall back to V1's "merge whole payload into
 * run.output" so existing behavior is preserved.
 */
export function applyFrameToRun(
  run: AppRun,
  payload: Record<string, unknown>,
): void {
  const ev = typeof payload.event === "string" ? payload.event : null;
  if (ev === null) {
    // Legacy / fixture path: spread the bare payload into output.
    run.output = { ...(run.output ?? {}), ...payload };
    return;
  }
  switch (ev) {
    case "result": {
      const out = payload.output;
      if (out !== undefined && out !== null && typeof out === "object") {
        run.output = {
          ...(run.output ?? {}),
          ...(out as Record<string, unknown>),
        };
      }
      return;
    }
    case "log": {
      const stream =
        typeof payload.stream === "string" ? payload.stream : "stdout";
      // Pack-emit({type:"log"}): `line` field. Process stderr chunk: `data`.
      const raw = payload.line ?? payload.data ?? payload.message;
      if (typeof raw === "string" && raw !== "") {
        // Stderr chunk arrives as a multi-line buffer; split so each line is
        // its own row (V1 logs panel behavior).
        for (const line of raw.split(/\r?\n/)) {
          if (line === "") continue;
          run.logs = [...run.logs, { stream, line }];
        }
      }
      return;
    }
    case "started": {
      const pid = payload.pid;
      if (typeof pid === "number") {
        run.pid = pid;
      }
      return;
    }
    case "terminated": {
      const code = payload.exit_code;
      if (typeof code === "number") {
        run.exitCode = code;
      }
      return;
    }
    case "status": {
      const hint = payload.hint;
      run.statusHint = typeof hint === "string" ? hint : null;
      // G4 — queue position (V1 `useAppBuilder.js:586-593` parity). The
      // backend emits a `status:queued` frame carrying `queuePosition`
      // (V1 camelCase field) while a second concurrent run waits for the
      // NPU lock; `queue_position` (snake_case) is also accepted for
      // forward-compat with any decoder that re-keys it. We only project
      // the position onto `run.queuePosition` — we deliberately do NOT
      // overwrite `run.status` here (the SSE `state` channel owns the run
      // status; racing it would thrash the card). DynamicOutput shows the
      // "排队第 N 位" card whenever `queuePosition > 0`, independent of the
      // DB-driven status (which is `streaming` while the runner waits for
      // the lock). Leaving the `queued` state clears the position
      // (V1 :591-593) so the card disappears once the run actually starts.
      const state = typeof payload.state === "string" ? payload.state : null;
      const posRaw = payload.queuePosition ?? payload.queue_position;
      if (state === "queued") {
        run.queuePosition = typeof posRaw === "number" ? posRaw : null;
      } else if (state !== null) {
        // Any non-queued status frame (preparing / running / …) means the
        // run left the queue: clear the position so "N ahead" disappears.
        run.queuePosition = null;
      } else if (typeof posRaw === "number") {
        // Defensive: a stateless status frame that still carries a position.
        run.queuePosition = posRaw;
      }
      return;
    }
    // 缺口 #6 — project the runner's `metrics` event onto `run.metrics`
    // so the live run (still streaming) shows the real inference latency
    // in the History "Inference" column, matching V1
    // (`useAppBuilder.js:601-606` sets `run.metrics = {latencyMs, …}`
    // from the `metrics` event). The backend also persists it and replays
    // it via `RunMetricsResponse.latency_ms` on reload (fetchMetrics), so
    // both the live and hydrated paths surface the value.
    case "metrics": {
      const lat = payload.latencyMs;
      if (typeof lat === "number" && Number.isFinite(lat) && lat >= 0) {
        run.metrics = {
          // Preserve any fields already hydrated from GET /metrics; fill
          // the required RunMetricsResponse keys from the live run when
          // this is the first metrics frame (no prior fetch).
          run_id: run.metrics?.run_id ?? run.id ?? "",
          status: run.metrics?.status ?? run.status,
          artifact_count: run.metrics?.artifact_count ?? 0,
          ...(run.metrics ?? {}),
          latencyMs: lat,
        };
      }
      return;
    }
    // progress / done / unknown_runner: no run-state mutation here;
    // `run.frames` keeps the raw payload for downstream projections
    // (overlay progress, MetricsView history).
    // Exception: `error` frame carries structured `detail` for diagnostics.
    case "error": {
      const detail = payload.detail;
      if (detail !== undefined && detail !== null && typeof detail === "object") {
        run.errorDetail = detail as Record<string, unknown>;
      }
      return;
    }
    default:
      return;
  }
}
