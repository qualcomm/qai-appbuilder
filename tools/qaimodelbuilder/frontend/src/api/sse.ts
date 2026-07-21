// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * QAI-native SSE client — `apiSSE`.
 *
 * Why fetch-based instead of `EventSource`:
 *   `EventSource` cannot attach custom request headers, which means it
 *   cannot carry our `X-QAI-CSRF` header (api-contract §6.3) and cannot
 *   pass through the dev proxy's CORS-clean `same-origin` credential.
 *   We hand-roll an SSE parser on top of `fetch` + `ReadableStream`.
 *
 * Wire format (api-contract §3):
 *   `event: <name>\n`
 *   `data:  <json>\n`
 *   `\n`            (frame separator)
 *   `: ping\n\n`    (heartbeat — IGNORED)
 *
 * Event names per route family:
 *   chat (§3.1):           message | error | done                 (+ ping)
 *   model_catalog (§3.2):  progress | error | done
 *   app_builder (§3.3):    state | frame | error | done
 *   ai_coding (§3.4):      message | error | done                 (+ optional ping)
 *
 * Terminator semantics:
 *   `done` and `error` are mutually exclusive — exactly one terminates
 *   the stream, after which the loop exits and the response body is
 *   consumed. `error` events trigger `onError(ApiError)` and then close.
 */

import type { ApiErrorPayload } from "@/types/streaming";

import { apiBaseUrl } from "./base";
import { ApiError, parseApiError } from "./errors";
import { attachCsrfHeader } from "./csrf";

/** Permissive event name type — concrete routes restrict these in their wrappers. */
export type SseEventName = string;

/** Handler bag — every callback is optional; unused events are silently ignored. */
export interface SseHandler {
  /** Catch-all — called for any non-terminator event with parsed JSON data. */
  readonly onEvent?: (event: SseEventName, data: unknown) => void;
  /** Called for `event: message` (chat / ai_coding). */
  readonly onMessage?: (data: unknown) => void;
  /** Called for `event: progress` (model_catalog). */
  readonly onProgress?: (data: unknown) => void;
  /** Called for `event: frame` (app_builder). */
  readonly onFrame?: (data: unknown) => void;
  /** Called for `event: state` (app_builder). */
  readonly onState?: (data: unknown) => void;
  /** Called once when the server sends `event: error`. Stream then closes. */
  readonly onError?: (err: ApiError) => void;
  /** Called once when the server sends `event: done`. */
  readonly onDone?: () => void;
}

/** Options for `apiSSE`. */
export interface SseOptions {
  /** Per-request `AbortSignal`. */
  readonly signal?: AbortSignal;
  /** Extra headers (CSRF is auto-attached if needed). */
  readonly headers?: Readonly<Record<string, string>> | Headers;
  /** Override credentials. Default `same-origin`. */
  readonly credentials?: "omit" | "same-origin" | "include";
  /**
   * Override HTTP method. Defaults to `GET`. The QAI SSE routes are all
   * `GET`, but tests sometimes need to flip this.
   */
  readonly method?: "GET" | "POST";
  /** Optional request body (only sensible with `method: "POST"`). */
  readonly body?: unknown;
  /**
   * Idle timeout in milliseconds. If no data (including pings/comments) is
   * received within this window, the stream is automatically aborted and an
   * `SseIdleTimeoutError` is thrown. Set to `0` or `Infinity` to disable.
   * Default: 120 000 ms (2 minutes).
   */
  readonly idleTimeoutMs?: number;
}

// ---------------------------------------------------------------------------
// URL builder (mirrors http.buildApiUrl but without query for brevity —
// SSE routes have very few query params; callers can pass them in `path`).
// ---------------------------------------------------------------------------

function buildSseUrl(path: string): string {
  if (/^https?:\/\//i.test(path) || path.startsWith("//")) return path;
  const base = apiBaseUrl();
  if (base === "") return path.startsWith("/") ? path : `/${path}`;
  return path.startsWith("/") ? `${base}${path}` : `${base}/${path}`;
}

// ---------------------------------------------------------------------------
// Wire parser — line buffer + frame buffer
// ---------------------------------------------------------------------------

/**
 * Result of parsing a single SSE frame from the buffered text.
 *   - `kind: "frame"` → a real event with `event` + `data`.
 *   - `kind: "comment"` → a heartbeat or empty comment, ignore.
 *   - `kind: "incomplete"` → buffer needs more bytes; do not advance.
 */
type ParsedFrame =
  | { kind: "frame"; event: string; data: string }
  | { kind: "comment" }
  | { kind: "incomplete" };

/**
 * Pull one frame off the buffer. Returns the parsed frame and the new
 * buffer (with the frame's bytes consumed). If the buffer doesn't yet
 * contain a complete frame (no `\n\n` boundary), returns `incomplete`.
 *
 * SSE allows both `\n` and `\r\n` line breaks; we normalise to `\n`.
 */
function pullFrame(buffer: string): {
  parsed: ParsedFrame;
  rest: string;
} {
  // Frames are separated by a blank line: `\n\n`. Locate the first one.
  const sepIdx = buffer.indexOf("\n\n");
  if (sepIdx < 0) {
    return { parsed: { kind: "incomplete" }, rest: buffer };
  }
  const raw = buffer.slice(0, sepIdx);
  const rest = buffer.slice(sepIdx + 2);

  // Each line is a field. Lines starting with `:` are comments (heartbeat).
  // We collapse multi-line `data:` per the SSE spec (newline-joined).
  let event = "message"; // SSE default per spec
  const dataLines: string[] = [];
  let isComment = true;

  for (const line of raw.split("\n")) {
    if (line === "") continue;
    if (line.startsWith(":")) continue; // comment line
    isComment = false;
    const colon = line.indexOf(":");
    let field: string;
    let value: string;
    if (colon < 0) {
      field = line;
      value = "";
    } else {
      field = line.slice(0, colon);
      value = line.slice(colon + 1);
      // Per SSE spec, a single leading space is stripped from value.
      if (value.startsWith(" ")) value = value.slice(1);
    }
    if (field === "event") {
      event = value;
    } else if (field === "data") {
      dataLines.push(value);
    } else {
      // `id` / `retry` / unknown — ignored for our use case.
    }
  }

  if (isComment) {
    return { parsed: { kind: "comment" }, rest };
  }
  return {
    parsed: { kind: "frame", event, data: dataLines.join("\n") },
    rest,
  };
}

/** Parse the data field as JSON; falls back to the raw string under `_raw`. */
function decodeData(data: string): unknown {
  if (data === "") return null;
  try {
    return JSON.parse(data);
  } catch {
    return { _raw: data };
  }
}

/** Type-guard for `ApiErrorPayload`. */
function isApiErrorPayload(v: unknown): v is ApiErrorPayload {
  if (v === null || typeof v !== "object") return false;
  const o = v as Record<string, unknown>;
  return (
    typeof o["type"] === "string" &&
    typeof o["code"] === "string" &&
    typeof o["message"] === "string"
  );
}

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

/**
 * Open a QAI-native SSE stream and dispatch envelopes to the handler.
 *
 * Returns a promise that resolves when the stream terminates normally
 * (received `event: done`, body EOF, or `signal` aborted) and rejects
 * if the stream produced `event: error` (handler is also notified) or
 * the underlying fetch failed (network / non-2xx pre-stream).
 *
 * The function intentionally does not type the frame payload — concrete
 * routes (chat / model_catalog / app_builder / ai_coding) wrap this in
 * a typed helper that asserts the right `frame_type` discriminator.
 */
export async function apiSSE(
  path: string,
  handler: SseHandler,
  opts: SseOptions = {},
): Promise<void> {
  const url = buildSseUrl(path);
  const method = opts.method ?? "GET";
  const headers = new Headers();
  headers.set("Accept", "text/event-stream");
  if (opts.body !== undefined && opts.body !== null) {
    headers.set("Content-Type", "application/json");
  }
  if (opts.headers !== undefined) {
    if (opts.headers instanceof Headers) {
      opts.headers.forEach((v, k) => headers.set(k, v));
    } else {
      for (const [k, v] of Object.entries(opts.headers)) {
        headers.set(k, v);
      }
    }
  }
  attachCsrfHeader(method, headers);

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      credentials: opts.credentials ?? "same-origin",
      signal: opts.signal,
      body:
        opts.body === undefined || opts.body === null
          ? null
          : JSON.stringify(opts.body),
    });
  } catch (cause) {
    throw await parseApiError(cause);
  }

  // Pre-stream errors arrive as a normal JSON envelope (api-contract §3.2).
  if (!response.ok) {
    throw await parseApiError(response);
  }

  // No body → treat as immediate completion.
  if (response.body === null) {
    handler.onDone?.();
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let terminated = false;
  let pendingError: ApiError | null = null;

  // Idle timeout — abort when no bytes arrive within the configured window.
  const idleTimeoutMs = opts.idleTimeoutMs ?? 120_000;
  const idleEnabled =
    Number.isFinite(idleTimeoutMs) && idleTimeoutMs > 0;
  let idleTimer: ReturnType<typeof setTimeout> | undefined;

  /** Reset (or start) the idle watchdog. */
  function resetIdleTimer(): void {
    if (!idleEnabled) return;
    if (idleTimer !== undefined) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      // Force-cancel the reader; the resulting rejection propagates to the
      // `reader.read()` await below, caught as AbortError or TypeError.
      void reader.cancel();
    }, idleTimeoutMs);
  }

  /** Clear the idle timer (used in finally). */
  function clearIdleTimer(): void {
    if (idleTimer !== undefined) {
      clearTimeout(idleTimer);
      idleTimer = undefined;
    }
  }

  try {
    // Kick off the idle timer before entering the loop.
    resetIdleTimer();

    while (!terminated) {
      let chunk: { done: boolean; value?: Uint8Array };
      try {
        chunk = await reader.read();
      } catch (cause) {
        // AbortError lands here when the consumer cancels the signal.
        const err = await parseApiError(cause);
        if (err.type !== "AbortError") {
          // If the idle timer triggered the cancel, surface a clear error.
          if (idleEnabled && idleTimer === undefined) {
            throw new ApiError(
              {
                type: "SseIdleTimeoutError",
                code: "client.sse_idle_timeout",
                message: `SSE stream idle for ${idleTimeoutMs}ms with no data received.`,
              },
              0,
            );
          }
          throw err;
        }
        // Check if the abort was caused by the idle timer (timer cleared
        // itself after firing, so idleTimer will be undefined here while
        // idleEnabled remains true — and the external signal was not aborted).
        if (
          idleEnabled &&
          idleTimer === undefined &&
          !(opts.signal?.aborted)
        ) {
          throw new ApiError(
            {
              type: "SseIdleTimeoutError",
              code: "client.sse_idle_timeout",
              message: `SSE stream idle for ${idleTimeoutMs}ms with no data received.`,
            },
            0,
          );
        }
        // Aborted: graceful exit, no error frame raised.
        return;
      }

      // Any data received — reset the idle watchdog.
      resetIdleTimer();

      if (chunk.done) {
        // Body EOF without `done` event — treat as graceful close. Some
        // routes terminate by closing the body (e.g. ai_coding when the
        // upstream provider hangs up). We do not synthesise `onDone()`
        // because the contract reserves it for explicit `event: done`.
        return;
      }
      if (chunk.value !== undefined) {
        buffer += decoder.decode(chunk.value, { stream: true });
      }

      // Drain all complete frames in the buffer.
      while (true) {
        const { parsed, rest } = pullFrame(buffer);
        if (parsed.kind === "incomplete") break;
        buffer = rest;
        if (parsed.kind === "comment") continue;

        const data = decodeData(parsed.data);
        const event = parsed.event;

        if (event === "done") {
          handler.onDone?.();
          terminated = true;
          break;
        }
        if (event === "error") {
          // Convert payload → ApiError → notify → stop the loop.
          let err: ApiError;
          if (isApiErrorPayload(data)) {
            err = new ApiError(data, 0);
          } else {
            err = new ApiError(
              {
                type: "MalformedSseError",
                code: "client.malformed_sse_error",
                message: "SSE error frame did not contain a valid envelope.",
                details: { data },
              },
              0,
            );
          }
          handler.onError?.(err);
          pendingError = err;
          terminated = true;
          break;
        }

        // Non-terminator events.
        handler.onEvent?.(event, data);
        switch (event) {
          case "message":
            handler.onMessage?.(data);
            break;
          case "progress":
            handler.onProgress?.(data);
            break;
          case "frame":
            handler.onFrame?.(data);
            break;
          case "state":
            handler.onState?.(data);
            break;
          default:
            // Unknown event names: silently ignored unless `onEvent` handled.
            break;
        }
      }
    }
  } finally {
    clearIdleTimer();
    try {
      await reader.cancel();
    } catch {
      // ignore — already done / aborted.
    }
  }

  if (pendingError !== null) {
    throw pendingError;
  }
}
