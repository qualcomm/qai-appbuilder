// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Pure formatting helpers for the Download Center.
 *
 * These mirror V1 (`useDownloadCenter.js`) byte-for-byte so the displayed
 * speed / size / ETA / version-sort behaviour matches the verified
 * reference. Extracted here so they're trivially unit-testable and reusable
 * by sub-composables / cards without leaking through `useDownloadCenter`.
 *
 * V1 source-of-truth references:
 *   formatBytes        useDownloadCenter.js:146-153
 *   formatSpeed        useDownloadCenter.js:155-158
 *   formatEta          useDownloadCenter.js:160-169
 *   compareVersionsDesc useDownloadCenter.js:117-127
 *   hasUnsafePath      DownloadCenterPanel.js:17-24
 *   _calcSpeed         useDownloadCenter.js:218-235  (sampling window 0.5s)
 */

import type { DownloadStatus } from "@/types/downloads";

/**
 * Human-readable byte size (V1 `formatBytes`).
 *
 * Rules (1024-base, units `[B, KB, MB, GB]`):
 *   - `n < 1024` → integer bytes (e.g. `512 B`).
 *   - Otherwise, divide by 1024 until < 1024 or units exhausted, format with
 *     1 decimal (e.g. `1.2 GB`).
 *   - `null` / `undefined` / non-finite → `"—"`.
 */
export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  if (n < 1024) return `${Math.floor(n)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

/**
 * Human-readable transfer speed (V1 `formatSpeed`).
 *
 * Returns `""` when the speed is non-positive (V1 elides the speed line in
 * that case rather than showing `0 B/s`).
 */
export function formatSpeed(bps: number | null | undefined): string {
  if (bps === null || bps === undefined || !Number.isFinite(bps) || bps <= 0) {
    return "";
  }
  return `${formatBytes(bps)}/s`;
}

/**
 * Human-readable ETA (V1 `formatEta`).
 *
 *   < 60s   → `"{ceil}s"`
 *   < 60m   → `"{m}m {ceil(s%60)}s"`
 *   ≥ 60m   → `"{h}h {m%60}m"`
 *
 * Returns `""` for non-positive / null / NaN values.
 */
export function formatEta(seconds: number | null | undefined): string {
  if (
    seconds === null ||
    seconds === undefined ||
    !Number.isFinite(seconds) ||
    seconds <= 0
  ) {
    return "";
  }
  const s = Math.ceil(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${Math.ceil(s % 60)}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/**
 * V1 `compareVersionsDesc` — descending semantic version sort.
 *
 * Splits on `.`, compares each segment numerically (NaN → 0), so newer
 * versions appear first.
 */
export function compareVersionsDesc(a: string, b: string): number {
  const pa = a.split(".");
  const pb = b.split(".");
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const na = Number.parseInt(pa[i] ?? "0", 10) || 0;
    const nb = Number.parseInt(pb[i] ?? "0", 10) || 0;
    if (na !== nb) return nb - na;
  }
  return 0;
}

/**
 * Detect a non-ASCII or whitespace-containing path (V1 `hasUnsafePath`).
 *
 * The Qualcomm QNN backend cannot load models from such paths; the UI
 * highlights this with a yellow warning banner + inline input warning so
 * users migrate to a clean path before downloading.
 *
 * Rule: any character with `charCodeAt > 127` OR `=== 32` (space).
 */
export function hasUnsafePath(p: string | null | undefined): boolean {
  if (typeof p !== "string" || p.length === 0) return false;
  for (let i = 0; i < p.length; i++) {
    const c = p.charCodeAt(i);
    if (c > 127 || c === 32) return true;
  }
  return false;
}

/**
 * Speed sampler state, kept *outside* the reactive download status to
 * avoid `{ ...state, downloaded_bytes }` spread overwriting `last_*`
 * fields between frames (V1 `_speedState`, useDownloadCenter.js:199-202).
 *
 * One entry per active task id.
 */
export interface SpeedSamplerState {
  last_bytes: number;
  last_time: number;
  speed_bps: number;
  eta_seconds: number;
}

/**
 * V1 `_calcSpeed` — sliding-window speed/ETA estimator (window ≥ 0.5s).
 *
 * Mutates `state` in place and returns `{speed_bps, eta_seconds}`.
 *
 * Algorithm (matches V1 byte-for-byte):
 *   - `dt = (now - last_time) / 1000` seconds.
 *   - Update only if `dt >= 0.5` (otherwise keep previous reading; this
 *     prevents jitter from sub-100ms SSE bursts).
 *   - `db = newBytes - last_bytes`. When `db > 0`, refresh `speed_bps =
 *     round(db / dt)`; otherwise keep the previous `speed_bps` (treat
 *     non-positive deltas as transient pauses, not a "stop" signal).
 *   - `eta_seconds = total > 0 && speed > 0 ? (total - newBytes) / speed : 0`.
 *   - On terminal status (`done`/`error`/`cancelled`) the caller should
 *     force `speed_bps = 0, eta_seconds = 0` (see useDownloadCenter).
 */
export function calcSpeed(
  state: SpeedSamplerState,
  newBytes: number,
  totalBytes: number,
  nowMs: number = Date.now(),
): { speed_bps: number; eta_seconds: number } {
  const dt = (nowMs - state.last_time) / 1000;
  if (dt >= 0.5) {
    const db = newBytes - state.last_bytes;
    if (db > 0) {
      state.speed_bps = Math.round(db / dt);
    }
    state.last_bytes = newBytes;
    state.last_time = nowMs;
  }
  if (totalBytes > 0 && state.speed_bps > 0) {
    state.eta_seconds = (totalBytes - newBytes) / state.speed_bps;
  } else {
    state.eta_seconds = 0;
  }
  return { speed_bps: state.speed_bps, eta_seconds: state.eta_seconds };
}

/** Initial sampler state for a freshly-started download. */
export function initialSpeedState(nowMs: number = Date.now()): SpeedSamplerState {
  return { last_bytes: 0, last_time: nowMs, speed_bps: 0, eta_seconds: 0 };
}

/**
 * V1 `getStatusClass` — status-derived CSS modifier name. Returned values:
 * `idle | preparing | downloading | done | error | cancelled`. (V2 uses the
 * raw enum value; this helper exists for parity with V1 components that
 * still pass through CSS class names.)
 */
export function statusClass(status: DownloadStatus): string {
  return status;
}

/** Whether a status represents an in-flight (non-terminal) download. */
export function isActiveStatus(status: DownloadStatus): boolean {
  return status === "preparing" || status === "downloading";
}

/** Whether a status represents a terminal (no further frames) download. */
export function isTerminalStatus(status: DownloadStatus): boolean {
  return status === "done" || status === "error" || status === "cancelled";
}
