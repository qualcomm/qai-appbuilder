// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Base URL helpers — kept in their own module so the higher-level
 * client modules (`http.ts`, `sse.ts`, `stream.ts`) can import them
 * without creating a cycle through `index.ts` (which re-exports the
 * full client surface).
 *
 * S5 PR-051. The originals were defined in `index.ts` during PR-050;
 * `index.ts` now re-exports these so existing imports are unchanged.
 */

/**
 * Resolve the API base URL.
 *
 * In dev, leaves the URL relative so the Vite proxy can route `/api/*`,
 * `/v1/*`, and `/api/chat/ws` to the backend (api-contract.md §1.1).
 * In production, honours `VITE_API_BASE_URL` if set, otherwise also
 * relative (FastAPI serves the SPA from the same origin).
 *
 * Hard-coded host:port literals are forbidden in source per the S5
 * spec §3 ("不得在前端代码里 fetch('http://localhost:...')"), so this
 * helper never returns one.
 */
export function apiBaseUrl(): string {
  const fromEnv = import.meta.env.VITE_API_BASE_URL;
  if (typeof fromEnv === "string" && fromEnv.length > 0) {
    return fromEnv.replace(/\/$/, "");
  }
  return "";
}

/**
 * Resolve the WebSocket base URL.
 *
 * Returns an `ws://` / `wss://` URL relative to the page origin when
 * `VITE_WS_BASE_URL` is unset, so the Vite dev proxy can transparently
 * forward `/api/chat/ws`.
 */
export function wsBaseUrl(): string {
  const fromEnv = import.meta.env.VITE_WS_BASE_URL;
  if (typeof fromEnv === "string" && fromEnv.length > 0) {
    return fromEnv.replace(/\/$/, "");
  }
  if (typeof window === "undefined") {
    return "";
  }
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}`;
}
