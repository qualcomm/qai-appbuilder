// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * HTTP client — non-streaming variants.
 *
 *   apiJson<TReq, TRes>(method, path, body?, opts?) → TRes
 *   apiRaw(method, path, opts?) → Response
 *   apiBlob(method, path, opts?) → Blob
 *   apiUpload<TRes>(path, formData, opts?) → TRes
 *
 * All four:
 *   - prepend `apiBaseUrl()` to relative paths so dev (Vite proxy) and
 *     prod (same-origin) both work without per-call wiring;
 *   - inject the `X-QAI-CSRF` header on state-mutating methods when the
 *     `qai_csrf` cookie is present (api-contract §6.3);
 *   - accept an `AbortSignal` via `opts.signal`, the foundation for the
 *     multi-tab Chat state machine in PR-054;
 *   - convert non-2xx responses into the right `ApiError` subclass via
 *     `parseApiError` (api-contract §2.2).
 */

import { apiBaseUrl } from "./base";
import { ApiError, UnauthorizedApiError, parseApiError } from "./errors";
import { attachCsrfHeader } from "./csrf";
import { redirectToLogin } from "./auth";

/** Common options accepted by every HTTP function. */
export interface ApiRequestOptions {
  /** Per-request `AbortSignal`. */
  readonly signal?: AbortSignal;
  /** Extra request headers (merged on top of defaults). */
  readonly headers?: Readonly<Record<string, string>> | Headers;
  /**
   * Extra query string. Keys with `undefined` are skipped; arrays are
   * encoded as repeated keys (`?k=a&k=b`); other values are coerced via
   * `String()`.
   */
  readonly query?: Readonly<Record<string, QueryValue>>;
  /**
   * `credentials` for the underlying `fetch`. Defaults to `"same-origin"`
   * which is the right value for same-origin (prod) and dev with the
   * Vite proxy (which preserves the origin).
   */
  readonly credentials?: "omit" | "same-origin" | "include";
}

/** Allowed query parameter values. `null` is dropped (alongside `undefined`). */
export type QueryValue =
  | string
  | number
  | boolean
  | null
  | undefined
  | ReadonlyArray<string | number | boolean>;

/** HTTP methods we generate convenience signatures for. */
export type ApiMethod =
  | "GET"
  | "HEAD"
  | "POST"
  | "PUT"
  | "PATCH"
  | "DELETE"
  | "OPTIONS";

// ---------------------------------------------------------------------------
// URL / header construction
// ---------------------------------------------------------------------------

/**
 * Build the full URL by joining `apiBaseUrl()` with `path` and the optional
 * query string. Absolute URLs (those that already start with `http(s)://`
 * or `//`) are returned unchanged — this is intentional so callers can
 * point at non-default origins explicitly when needed (still subject to
 * the host:port lint rule, which exempts `vite.config.ts` only).
 */
export function buildApiUrl(
  path: string,
  query: ApiRequestOptions["query"],
): string {
  let url: string;
  if (/^https?:\/\//i.test(path) || path.startsWith("//")) {
    url = path;
  } else {
    const base = apiBaseUrl();
    if (base === "") {
      url = path.startsWith("/") ? path : `/${path}`;
    } else {
      url = path.startsWith("/") ? `${base}${path}` : `${base}/${path}`;
    }
  }
  const qs = encodeQuery(query);
  if (qs === "") return url;
  return url.includes("?") ? `${url}&${qs}` : `${url}?${qs}`;
}

function encodeQuery(query: ApiRequestOptions["query"]): string {
  if (query === undefined) return "";
  const parts: string[] = [];
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item === undefined || item === null) continue;
        parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(item))}`);
      }
    } else {
      parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
    }
  }
  return parts.join("&");
}

/**
 * Merge user-provided headers + content-type defaults + CSRF.
 * `applyContentType` is `true` for JSON requests, `false` for `Blob` /
 * `Raw` (so callers can set whatever Accept they want) and irrelevant
 * for FormData (the browser computes the boundary).
 */
function buildHeaders(
  method: string,
  user: ApiRequestOptions["headers"],
  contentType: string | null,
  acceptJson: boolean,
): Headers {
  const headers = new Headers();
  if (acceptJson) {
    headers.set("Accept", "application/json");
  }
  if (contentType !== null) {
    headers.set("Content-Type", contentType);
  }
  if (user !== undefined) {
    if (user instanceof Headers) {
      user.forEach((v, k) => headers.set(k, v));
    } else {
      for (const [k, v] of Object.entries(user)) {
        headers.set(k, v);
      }
    }
  }
  attachCsrfHeader(method, headers);
  return headers;
}

// ---------------------------------------------------------------------------
// SSO 401 interceptor
// ---------------------------------------------------------------------------

/**
 * Handler invoked when a protected request returns the SSO 401 envelope
 * (`code === "auth.required"`). Registered once from `App.vue` with the
 * auth store's `promptLogin()`. Kept as an injected callback (rather than
 * importing the pinia store here) so this low-level HTTP module stays
 * free of a store dependency / circular-import / "pinia not active yet"
 * hazard. Defaults to a no-op until registered.
 */
let _authRequiredHandler: (() => void) | null = null;

/** Register the "authentication required" handler (App.vue → store.promptLogin). */
export function setAuthRequiredHandler(handler: (() => void) | null): void {
  _authRequiredHandler = handler;
}

/**
 * Wrap `parseApiError` with a single side-effect: when the backend
 * signals "you need to sign in" via the SSO envelope (401 with
 * `code === "auth.required"`), invoke the registered auth-required
 * handler, which shows the in-app login-prompt modal (`LoginPrompt.vue`)
 * — we deliberately do NOT hard-redirect the whole page to Okta here
 * (that was jarring; the SPA now stays rendered behind a modal). The
 * caller still receives the parsed `UnauthorizedApiError`.
 *
 * Gated on `typeof window` so vitest / SSR paths are unaffected, and on
 * not being inside the server-driven login flow paths.
 */
async function parseAndInterceptApiError(input: unknown): Promise<ApiError> {
  const err = await parseApiError(input);
  if (
    typeof window !== "undefined" &&
    err instanceof UnauthorizedApiError &&
    err.code === "auth.required"
  ) {
    const path = window.location.pathname;
    const inLoginFlow =
      path === "/auth/login" ||
      path === "/callback" ||
      path === "/auth/logout" ||
      path === "/auth/signed-out";
    if (!inLoginFlow) {
      if (_authRequiredHandler !== null) {
        _authRequiredHandler();
      } else {
        // No handler registered yet (very early boot) — fall back to the
        // server login redirect so the user is never stranded.
        redirectToLogin();
      }
    }
  }
  return err;
}

// ---------------------------------------------------------------------------
// apiRaw — bare Response
// ---------------------------------------------------------------------------

/**
 * Issue a request and return the raw `Response`. Useful when the caller
 * wants to handle non-2xx themselves or read the body in a non-standard
 * way (e.g. streaming a checksum verification chunk-by-chunk).
 *
 * Network / abort errors propagate as `ApiError` (status `0`). 4xx/5xx
 * responses are returned successfully — handling them is the caller's
 * responsibility.
 */
export async function apiRaw(
  method: ApiMethod,
  path: string,
  opts: ApiRequestOptions = {},
): Promise<Response> {
  const url = buildApiUrl(path, opts.query);
  const headers = buildHeaders(method, opts.headers, null, false);
  try {
    return await fetch(url, {
      method,
      headers,
      credentials: opts.credentials ?? "same-origin",
      signal: opts.signal,
    });
  } catch (cause) {
    throw await parseAndInterceptApiError(cause);
  }
}

// ---------------------------------------------------------------------------
// apiJson — JSON in / JSON out
// ---------------------------------------------------------------------------

/**
 * Issue a JSON request and parse the JSON response.
 *
 * Generic constraints:
 *   - `TReq` is the request body shape; pass `void`/`undefined` for none.
 *   - `TRes` is the response body shape.
 *
 * Behaviour:
 *   - Body is `JSON.stringify`-d when `body !== undefined`.
 *   - For 204 No Content, returns `undefined` cast as `TRes` — callers
 *     who target 204 routes should declare `TRes = void`.
 *   - Non-2xx → throws `ApiError` (subclass per status).
 *   - Network / abort → throws `ApiError` (status 0).
 */
export async function apiJson<TRes = unknown, TReq = unknown>(
  method: ApiMethod,
  path: string,
  body?: TReq,
  opts: ApiRequestOptions = {},
): Promise<TRes> {
  const url = buildApiUrl(path, opts.query);
  const headers = buildHeaders(
    method,
    opts.headers,
    body === undefined ? null : "application/json",
    true,
  );
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      credentials: opts.credentials ?? "same-origin",
      signal: opts.signal,
      body: body === undefined ? null : JSON.stringify(body),
    });
  } catch (cause) {
    throw await parseAndInterceptApiError(cause);
  }
  if (!response.ok) {
    throw await parseAndInterceptApiError(response);
  }
  if (response.status === 204) {
    return undefined as unknown as TRes;
  }
  // 200-class with body. Some routes return empty body even on 200; tolerate.
  const text = await response.text();
  if (text === "") {
    return undefined as unknown as TRes;
  }
  try {
    return JSON.parse(text) as TRes;
  } catch (cause) {
    throw new ApiError(
      {
        type: "MalformedJsonResponse",
        code: "client.malformed_json_response",
        message: "Server returned non-JSON body where JSON was expected.",
        details: { rawText: text, parseError: (cause as Error).message },
      },
      response.status,
    );
  }
}

// ---------------------------------------------------------------------------
// apiBlob — binary fetch
// ---------------------------------------------------------------------------

/**
 * Fetch a binary resource (e.g. artifact blob, audio file, image).
 *
 * Non-2xx → throws `ApiError` (parsed from JSON body if any). 2xx →
 * returns the `Blob` (`response.blob()` honours the server's
 * `Content-Type`).
 */
export async function apiBlob(
  method: ApiMethod,
  path: string,
  opts: ApiRequestOptions = {},
): Promise<Blob> {
  const url = buildApiUrl(path, opts.query);
  const headers = buildHeaders(method, opts.headers, null, false);
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      credentials: opts.credentials ?? "same-origin",
      signal: opts.signal,
    });
  } catch (cause) {
    throw await parseAndInterceptApiError(cause);
  }
  if (!response.ok) {
    throw await parseAndInterceptApiError(response);
  }
  return await response.blob();
}

// ---------------------------------------------------------------------------
// apiUpload — multipart/form-data
// ---------------------------------------------------------------------------

/**
 * Submit a `FormData` body to a route that accepts multipart uploads
 * (e.g. `/api/app-builder/upload/audio`).
 *
 * Notes:
 *   - The `Content-Type` is computed automatically by the browser
 *     (`multipart/form-data; boundary=...`); we DO NOT set it.
 *   - Method is always `POST` per the FastAPI / starlette convention
 *     for multipart endpoints.
 *   - Response is parsed as JSON; non-2xx → `ApiError`.
 */
export async function apiUpload<TRes = unknown>(
  path: string,
  formData: FormData,
  opts: ApiRequestOptions = {},
): Promise<TRes> {
  const method: ApiMethod = "POST";
  const url = buildApiUrl(path, opts.query);
  // Pass `null` for content-type so the browser sets it with the boundary.
  const headers = buildHeaders(method, opts.headers, null, true);
  // Belt-and-braces: if the caller pre-set Content-Type, drop it — the
  // boundary would be wrong.
  if (headers.has("Content-Type")) {
    headers.delete("Content-Type");
  }
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      credentials: opts.credentials ?? "same-origin",
      signal: opts.signal,
      body: formData,
    });
  } catch (cause) {
    throw await parseAndInterceptApiError(cause);
  }
  if (!response.ok) {
    throw await parseAndInterceptApiError(response);
  }
  if (response.status === 204) {
    return undefined as unknown as TRes;
  }
  const text = await response.text();
  if (text === "") {
    return undefined as unknown as TRes;
  }
  try {
    return JSON.parse(text) as TRes;
  } catch (cause) {
    throw new ApiError(
      {
        type: "MalformedJsonResponse",
        code: "client.malformed_json_response",
        message: "Server returned non-JSON body where JSON was expected.",
        details: { rawText: text, parseError: (cause as Error).message },
      },
      response.status,
    );
  }
}
