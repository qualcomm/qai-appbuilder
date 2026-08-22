// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Unified API error model — frontend mirror of `qai.platform.errors`.
 *
 * The backend wraps every error in a single JSON envelope (api-contract.md
 * §2.1):
 *
 *   { type, code, message, details? }
 *
 * mapped from `QaiError.to_dict()`. The HTTP status code (api-contract.md
 * §2.2) discriminates the python source exception. We materialise that
 * discrimination on the frontend with a small class hierarchy so call
 * sites can `instanceof RateLimitedApiError` rather than peek at numeric
 * codes.
 *
 * Design notes (PR-051):
 *   - All classes extend `ApiError` (which extends `Error`).
 *   - Status code is captured on the instance (`status`).
 *   - `RateLimitedApiError` exposes `retryAfterSeconds`, sourced from
 *     the `Retry-After` HTTP header first (api-contract.md §2.3) and the
 *     `details.retry_after_s` body field as fallback.
 *   - `parseApiError` is the single entry point — accepts `Response`,
 *     a body-shaped object, an `Error`, or any unknown — and never throws.
 */

import type { ApiErrorPayload } from "@/types/streaming";

// ---------------------------------------------------------------------------
// Class hierarchy
// ---------------------------------------------------------------------------

/** Sentinel value used when the source has no HTTP status (e.g. network error). */
export const NO_STATUS = 0 as const;

/**
 * Base API error. Concrete subclasses fix the HTTP status; this class is
 * also used directly for unknown statuses outside the §2.2 mapping.
 */
export class ApiError extends Error {
  public readonly type: string;
  public readonly code: string;
  public readonly status: number;
  public readonly details: Readonly<Record<string, unknown>> | undefined;

  public constructor(
    payload: ApiErrorPayload,
    status: number,
  ) {
    super(payload.message);
    this.name = "ApiError";
    this.type = payload.type;
    this.code = payload.code;
    this.status = status;
    this.details = payload.details;
    // Restore prototype for `instanceof` after transpilation down-target.
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export class ValidationApiError extends ApiError {
  public constructor(payload: ApiErrorPayload) {
    super(payload, 400);
    this.name = "ValidationApiError";
    Object.setPrototypeOf(this, ValidationApiError.prototype);
  }
}

export class UnauthorizedApiError extends ApiError {
  public constructor(payload: ApiErrorPayload) {
    super(payload, 401);
    this.name = "UnauthorizedApiError";
    Object.setPrototypeOf(this, UnauthorizedApiError.prototype);
  }
}

export class ForbiddenApiError extends ApiError {
  public constructor(payload: ApiErrorPayload) {
    super(payload, 403);
    this.name = "ForbiddenApiError";
    Object.setPrototypeOf(this, ForbiddenApiError.prototype);
  }
}

export class NotFoundApiError extends ApiError {
  public constructor(payload: ApiErrorPayload) {
    super(payload, 404);
    this.name = "NotFoundApiError";
    Object.setPrototypeOf(this, NotFoundApiError.prototype);
  }
}

export class ConflictApiError extends ApiError {
  public constructor(payload: ApiErrorPayload) {
    super(payload, 409);
    this.name = "ConflictApiError";
    Object.setPrototypeOf(this, ConflictApiError.prototype);
  }
}

export class PreconditionFailedApiError extends ApiError {
  public constructor(payload: ApiErrorPayload) {
    super(payload, 412);
    this.name = "PreconditionFailedApiError";
    Object.setPrototypeOf(this, PreconditionFailedApiError.prototype);
  }
}

export class RateLimitedApiError extends ApiError {
  /**
   * Retry-After in seconds. Populated from (in order):
   *   1. The `Retry-After` HTTP response header (api-contract.md §2.3).
   *   2. `details.retry_after_s` from the JSON body.
   *   3. `null` if neither is present.
   */
  public readonly retryAfterSeconds: number | null;

  public constructor(payload: ApiErrorPayload, retryAfterSeconds: number | null) {
    super(payload, 429);
    this.name = "RateLimitedApiError";
    this.retryAfterSeconds = retryAfterSeconds;
    Object.setPrototypeOf(this, RateLimitedApiError.prototype);
  }
}

export class DomainApiError extends ApiError {
  public constructor(payload: ApiErrorPayload) {
    super(payload, 422);
    this.name = "DomainApiError";
    Object.setPrototypeOf(this, DomainApiError.prototype);
  }
}

export class InfrastructureApiError extends ApiError {
  public constructor(payload: ApiErrorPayload) {
    super(payload, 503);
    this.name = "InfrastructureApiError";
    Object.setPrototypeOf(this, InfrastructureApiError.prototype);
  }
}

export class UnknownApiError extends ApiError {
  public constructor(payload: ApiErrorPayload, status: number) {
    super(payload, status);
    this.name = "UnknownApiError";
    Object.setPrototypeOf(this, UnknownApiError.prototype);
  }
}

// ---------------------------------------------------------------------------
// parseApiError — public entry point
// ---------------------------------------------------------------------------

/** Type-guard: shape matches the §2.1 envelope. */
function isApiErrorPayload(value: unknown): value is ApiErrorPayload {
  if (value === null || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v["type"] === "string" &&
    typeof v["code"] === "string" &&
    typeof v["message"] === "string"
  );
}

/** Build a "fallback" envelope when the real one is unavailable / malformed. */
function fallbackPayload(
  type: string,
  code: string,
  message: string,
  details?: Record<string, unknown>,
): ApiErrorPayload {
  if (details === undefined) {
    return { type, code, message };
  }
  return { type, code, message, details };
}

/** Coerce arbitrary body into an `ApiErrorPayload` (with safe fallbacks). */
function coercePayload(body: unknown, status: number): ApiErrorPayload {
  if (isApiErrorPayload(body)) {
    return body;
  }
  // Body present but malformed: keep raw under details.body.
  return fallbackPayload(
    "MalformedErrorEnvelope",
    "client.malformed_error_envelope",
    `HTTP ${status} returned a non-conforming error body.`,
    { body },
  );
}

/**
 * Parse the `Retry-After` header value as integer seconds.
 *
 * Per RFC 7231 §7.1.3 the value can be either a non-negative integer
 * (seconds) or an HTTP-date. Backend §2.3 always emits seconds-as-int,
 * but we parse defensively.
 */
function parseRetryAfterHeader(headerValue: string | null | undefined): number | null {
  if (headerValue === null || headerValue === undefined) return null;
  const trimmed = headerValue.trim();
  if (trimmed === "") return null;
  // Numeric seconds first.
  if (/^\d+(?:\.\d+)?$/.test(trimmed)) {
    const n = Number(trimmed);
    return Number.isFinite(n) && n >= 0 ? n : null;
  }
  // HTTP-date fallback.
  const t = Date.parse(trimmed);
  if (Number.isNaN(t)) return null;
  const delta = (t - Date.now()) / 1000;
  return delta >= 0 ? delta : 0;
}

/**
 * Best-effort retry-after extraction: header first, body fallback.
 */
function extractRetryAfter(
  payload: ApiErrorPayload,
  headerValue: string | null | undefined,
): number | null {
  const fromHeader = parseRetryAfterHeader(headerValue);
  if (fromHeader !== null) return fromHeader;
  const details = payload.details;
  if (details && typeof details["retry_after_s"] === "number") {
    const v = details["retry_after_s"];
    if (Number.isFinite(v) && v >= 0) return v;
  }
  return null;
}

/** Build the right concrete subclass for an §2.2 status code. */
function buildFromStatus(
  status: number,
  payload: ApiErrorPayload,
  retryAfterHeader: string | null | undefined,
): ApiError {
  switch (status) {
    case 400:
      return new ValidationApiError(payload);
    case 401:
      return new UnauthorizedApiError(payload);
    case 403:
      return new ForbiddenApiError(payload);
    case 404:
      return new NotFoundApiError(payload);
    case 409:
      return new ConflictApiError(payload);
    case 412:
      return new PreconditionFailedApiError(payload);
    case 422:
      return new DomainApiError(payload);
    case 429:
      return new RateLimitedApiError(
        payload,
        extractRetryAfter(payload, retryAfterHeader),
      );
    case 503:
      return new InfrastructureApiError(payload);
    default:
      return new UnknownApiError(payload, status);
  }
}

/**
 * Parse an arbitrary error-shaped value into a concrete `ApiError`.
 *
 * Accepted inputs:
 *   - `Response` — body is read as JSON; status header drives the class.
 *   - JSON object matching `ApiErrorPayload` — uses `0` as the status.
 *   - `Error` — wrapped as a network/client error (status `0`).
 *   - anything else — wrapped as `UnknownApiError` with status `0`.
 *
 * This function never throws; on any parse failure it returns a best-effort
 * `ApiError` carrying enough metadata to debug.
 */
export async function parseApiError(input: unknown): Promise<ApiError> {
  // Path 1: Response — read status + body + Retry-After header.
  if (input instanceof Response) {
    const status = input.status;
    const retryAfterHeader = input.headers.get("Retry-After");
    let body: unknown = null;
    try {
      // `clone()` so callers can still consume the response if they want.
      body = await input.clone().json();
    } catch {
      // Non-JSON or empty body: fall through with `null`.
      try {
        const text = await input.clone().text();
        body = text === "" ? null : { rawText: text };
      } catch {
        body = null;
      }
    }
    const payload =
      body === null
        ? fallbackPayload(
            "EmptyErrorBody",
            "client.empty_error_body",
            `HTTP ${status} with no parseable body.`,
          )
        : coercePayload(body, status);
    return buildFromStatus(status, payload, retryAfterHeader);
  }

  // Path 2: Already-shaped envelope (e.g. WS / SSE error frame).
  if (isApiErrorPayload(input)) {
    return new ApiError(input, NO_STATUS);
  }

  // Path 3: Raw Error (network failure, AbortError, TypeError, ...).
  if (input instanceof Error) {
    const isAbort = input.name === "AbortError";
    return new ApiError(
      fallbackPayload(
        isAbort ? "AbortError" : input.name || "Error",
        isAbort ? "client.aborted" : "client.network_error",
        input.message || (isAbort ? "Request aborted." : "Network error."),
      ),
      NO_STATUS,
    );
  }

  // Path 4: Anything else.
  return new ApiError(
    fallbackPayload(
      "UnknownError",
      "client.unknown",
      "Unknown error of non-Error / non-Response type.",
      { value: safeStringify(input) },
    ),
    NO_STATUS,
  );
}

/** JSON.stringify with cycle / non-serialisable safety. */
function safeStringify(v: unknown): string {
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}
