// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * OpenAI Compat streaming client — `apiStream`.
 *
 * Used for `POST /v1/chat/completions` with `stream: true`. The wire
 * format is intentionally *different* from QAI-native SSE (api-contract
 * §3.5) because third-party clients (`openai` SDK, LangChain, ...) hard-
 * code this exact parser:
 *
 *   data: {"id":"...","object":"chat.completion.chunk", ...}
 *
 *   data: {"id":"...","object":"chat.completion.chunk", ...}
 *
 *   data: [DONE]
 *
 * Differences vs `apiSSE` (§3 vs §3.5):
 *   - No `event:` lines — every line is a `data:` line.
 *   - Terminator is the literal `data: [DONE]`, NOT `event: done`.
 *   - Errors are returned as a normal HTTP non-2xx response with the
 *     unified QAI error envelope — NOT as in-stream frames. We let
 *     `parseApiError` handle that path before reading any body.
 *
 * Method is `POST` (the only OpenAI streaming verb) and body is
 * `application/json` (the request payload).
 */

import { apiBaseUrl } from "./base";
import { ApiError, parseApiError } from "./errors";
import { attachCsrfHeader } from "./csrf";

/** Handler called for each parsed `data:` JSON object until `[DONE]`. */
export interface StreamHandler<TFrame> {
  /** Called for each chunk (a parsed JSON object preceding the [DONE] marker). */
  readonly onChunk?: (chunk: TFrame) => void;
  /** Called once when the literal `data: [DONE]` terminator is seen. */
  readonly onDone?: () => void;
  /** Called if a chunk fails JSON parsing — defaults to throwing. */
  readonly onParseError?: (line: string, cause: unknown) => void;
}

/** Options for `apiStream`. */
export interface StreamOptions<TBody = unknown> {
  /** Per-request `AbortSignal`. */
  readonly signal?: AbortSignal;
  /** Extra headers (CSRF auto-attached). */
  readonly headers?: Readonly<Record<string, string>> | Headers;
  /** Override credentials (default `same-origin`). */
  readonly credentials?: "omit" | "same-origin" | "include";
  /** Optional method override (defaults to `POST`). */
  readonly method?: "POST" | "GET";
  /** Request body. Stringified as JSON (defaults to no body). */
  readonly body?: TBody;
  /**
   * Idle timeout in milliseconds — if no bytes arrive within this window the
   * stream reader is cancelled and a `StreamIdleTimeoutError` is raised.
   * Defaults to 120 000 ms (2 minutes). Set to `Infinity` or `0` to disable.
   */
  readonly idleTimeoutMs?: number;
}

function buildStreamUrl(path: string): string {
  if (/^https?:\/\//i.test(path) || path.startsWith("//")) return path;
  const base = apiBaseUrl();
  if (base === "") return path.startsWith("/") ? path : `/${path}`;
  return path.startsWith("/") ? `${base}${path}` : `${base}/${path}`;
}

/** The literal terminator used by OpenAI `chat/completions` streaming. */
const DONE_MARKER = "[DONE]";

/**
 * Issue an OpenAI-compat streaming request and dispatch each chunk.
 *
 * Resolves on `[DONE]` (or body EOF, treated as graceful close) and
 * rejects on:
 *   - non-2xx response (parsed as a QAI `ApiError` envelope),
 *   - network / abort error (wrapped as `ApiError` with status `0`),
 *   - JSON parse error if no `onParseError` handler is supplied.
 */
export async function apiStream<TFrame, TBody = unknown>(
  path: string,
  handler: StreamHandler<TFrame>,
  opts: StreamOptions<TBody> = {},
): Promise<void> {
  const url = buildStreamUrl(path);
  const method = opts.method ?? "POST";
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

  if (!response.ok) {
    throw await parseApiError(response);
  }

  if (response.body === null) {
    handler.onDone?.();
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let terminated = false;

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
        const err = await parseApiError(cause);
        if (err.type !== "AbortError") {
          // If the idle timer triggered the cancel, surface a clear error.
          if (idleEnabled && idleTimer === undefined) {
            throw new ApiError(
              {
                type: "StreamIdleTimeoutError",
                code: "client.stream_idle_timeout",
                message: `Stream idle for ${idleTimeoutMs}ms with no data received.`,
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
              type: "StreamIdleTimeoutError",
              code: "client.stream_idle_timeout",
              message: `Stream idle for ${idleTimeoutMs}ms with no data received.`,
            },
            0,
          );
        }
        return;
      }

      // Any data received — reset the idle watchdog.
      resetIdleTimer();

      if (chunk.done) {
        return; // body EOF without `[DONE]`: graceful close, no onDone.
      }
      if (chunk.value !== undefined) {
        buffer += decoder.decode(chunk.value, { stream: true });
      }

      // Frames are `\n\n`-separated per SSE convention; OpenAI wire format
      // emits them as `data: <json>\n\n`. Drain all complete frames.
      while (true) {
        const sepIdx = buffer.indexOf("\n\n");
        if (sepIdx < 0) break;
        const raw = buffer.slice(0, sepIdx);
        buffer = buffer.slice(sepIdx + 2);

        // A frame may have multiple lines but in practice OpenAI sends
        // exactly one `data:` line per frame. Concatenate just in case.
        const dataParts: string[] = [];
        for (const line of raw.split("\n")) {
          if (line === "") continue;
          if (line.startsWith(":")) continue; // comment
          if (!line.startsWith("data:")) continue;
          let value = line.slice(5);
          if (value.startsWith(" ")) value = value.slice(1);
          dataParts.push(value);
        }
        if (dataParts.length === 0) continue;
        const dataLine = dataParts.join("\n");

        if (dataLine === DONE_MARKER) {
          handler.onDone?.();
          terminated = true;
          break;
        }

        try {
          const parsed = JSON.parse(dataLine) as TFrame;
          handler.onChunk?.(parsed);
        } catch (cause) {
          if (handler.onParseError !== undefined) {
            handler.onParseError(dataLine, cause);
            continue;
          }
          throw new ApiError(
            {
              type: "MalformedJsonChunk",
              code: "client.malformed_json_chunk",
              message: "Stream chunk is not valid JSON.",
              details: { line: dataLine },
            },
            response.status,
          );
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
}
