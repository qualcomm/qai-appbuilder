// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * CSRF cookie / header utilities (api-contract.md §6.3).
 *
 * The legacy and the future S4 middleware both use:
 *   cookie name: `qai_csrf`
 *   header name: `X-QAI-CSRF`
 *
 * Names are LOCKED — see api-contract §6.3 (must not change). All
 * non-GET / non-HEAD requests through `apiJson` / `apiRaw` / `apiBlob` /
 * `apiUpload` automatically attach the header when the cookie is
 * present. Missing cookie does NOT throw — the backend will respond
 * 401/403 if it cares, which propagates as a normal `ApiError`.
 */

/** The cookie name set by the backend. NEVER change this. */
export const QAI_CSRF_COOKIE = "qai_csrf";

/** The HTTP request header name carrying the CSRF token. NEVER change. */
export const QAI_CSRF_HEADER = "X-QAI-CSRF";

/**
 * HTTP methods that DO require CSRF protection (state-mutating).
 * Per RFC 7231 §4.2.1, GET and HEAD are safe and excluded.
 */
const PROTECTED_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** Returns `true` if `method` requires the CSRF header to be attached. */
export function methodNeedsCsrf(method: string): boolean {
  return PROTECTED_METHODS.has(method.toUpperCase());
}

/**
 * Read the value of `qai_csrf` from `document.cookie`. Returns `null`
 * if the cookie is missing, the value is empty, or `document` is not
 * defined (e.g. SSR / Node test environments without happy-dom).
 */
export function readCsrfCookie(): string | null {
  if (typeof document === "undefined") return null;
  const raw = document.cookie;
  if (typeof raw !== "string" || raw.length === 0) return null;
  // Cookies are `; `-separated. Match precisely on the name.
  const parts = raw.split(";");
  for (const part of parts) {
    const trimmed = part.trim();
    if (trimmed.length === 0) continue;
    const eq = trimmed.indexOf("=");
    if (eq < 0) continue;
    const name = trimmed.slice(0, eq).trim();
    if (name !== QAI_CSRF_COOKIE) continue;
    const value = decodeURIComponent(trimmed.slice(eq + 1));
    return value === "" ? null : value;
  }
  return null;
}

/**
 * Mutate a `Headers` instance to inject the CSRF header when the method
 * requires it AND the cookie is present.
 *
 * The header is NEVER overwritten if the caller already set it
 * explicitly, so tests / advanced callers can override the auto value.
 */
export function attachCsrfHeader(method: string, headers: Headers): void {
  if (!methodNeedsCsrf(method)) return;
  if (headers.has(QAI_CSRF_HEADER)) return;
  const token = readCsrfCookie();
  if (token === null) return;
  headers.set(QAI_CSRF_HEADER, token);
}
