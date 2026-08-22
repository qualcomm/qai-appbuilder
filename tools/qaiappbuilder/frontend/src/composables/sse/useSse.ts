// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useSse` — generic per-context SSE composable foundation.
 *
 * S7.5 L8 PR-804.
 *
 * Architecture decision (P0-FR9): replace the legacy single global
 * `/api/events` SSE channel with one composable per bounded context.
 * Each context owns its own connection lifecycle (open / abort / retry
 * with backoff) and hands typed event payloads to its consumer SFC.
 * The four concrete contexts that ship in PR-804 are:
 *
 *   - chat            → `event: message | error | done`         (api-contract §3.1)
 *   - model_catalog   → `event: progress | error | done`        (api-contract §3.2)
 *   - app_builder     → `event: state | frame | error | done`   (api-contract §3.3)
 *   - ai_coding       → `event: message | error | done`         (api-contract §3.4)
 *
 * The four `useSse{Chat,Downloads,AppBuilder,AiCoding}` files in this
 * directory wrap this foundation with the concrete event-name set, the
 * backend route path, and the typed payload contract. Consumer SFCs
 * never see this file directly; they call the typed wrapper.
 */
import { onScopeDispose, ref, type Ref } from "vue";

import { apiSSE, type SseHandler, type SseOptions } from "@/api/sse";
import { apiWsStream, type WsStreamOptions } from "@/api/wsStream";
import type { ApiError } from "@/api/errors";

/**
 * Connection state machine.
 *
 *   idle → connecting → open → (closed | failed)
 *
 *   * `idle`        — never opened, or last connection completed cleanly.
 *   * `connecting`  — `apiSSE` is in flight; first frame not yet seen.
 *   * `open`        — at least one non-comment frame has been received.
 *   * `closed`      — server sent `event: done` or body EOF.
 *   * `failed`      — server sent `event: error` or fetch raised.
 */
export type SseConnectionState =
  | "idle"
  | "connecting"
  | "open"
  | "closed"
  | "failed";

export interface UseSseOptions extends SseOptions {
  /**
   * Auto-reconnect backoff schedule in ms. The composable retries
   * `failed` connections after the first / second / … delay, then
   * gives up after exhausting the array. Set `[]` to disable retries.
   *
   * Defaults to `[]` — callers opt in. Connection lifecycle is
   * deterministic by default; reconnection is a UX policy decision
   * the consumer SFC owns.
   */
  readonly retryDelaysMs?: readonly number[];
  /**
   * WebSocket path for WS-first transport. When provided, the composable
   * uses `apiWsStream` (WS-first with SSE fallback) instead of plain
   * `apiSSE`. The SSE `path` argument becomes the fallback path.
   */
  readonly wsPath?: string | (() => string);
}

export interface UseSseReturn {
  readonly state: Ref<SseConnectionState>;
  readonly lastError: Ref<ApiError | null>;
  /** Open the stream. Idempotent — does nothing if already connecting/open. */
  readonly open: () => Promise<void>;
  /** Abort the in-flight stream (or no-op if idle). */
  readonly close: () => void;
  /**
   * For tests: synchronously injects a fake state value so callers can
   * assert `state.value === 'failed'` etc. without spinning up a real
   * connection. This is a getter not exposed in production code paths.
   */
}

/**
 * Build a per-stream composable around a fixed (path, handler-shape) pair.
 *
 * The caller's `handlerFactory` is invoked on every `open()` to mint a
 * fresh handler (typically closing over reactive refs that record
 * frame data). Returning a closure here keeps the composable's
 * `open` / `close` lifecycle independent of the consumer's reactive
 * graph, which is important under HMR / route navigation.
 */
export function useSse(
  path: string | (() => string),
  handlerFactory: () => SseHandler,
  opts: UseSseOptions = {},
): UseSseReturn {
  const state: Ref<SseConnectionState> = ref("idle");
  const lastError: Ref<ApiError | null> = ref(null);
  const retries = opts.retryDelaysMs ?? [];

  let abort: AbortController | null = null;
  let attempt = 0;
  let scheduledRetry: ReturnType<typeof setTimeout> | null = null;

  function clearScheduledRetry(): void {
    if (scheduledRetry !== null) {
      clearTimeout(scheduledRetry);
      scheduledRetry = null;
    }
  }

  async function runOnce(): Promise<void> {
    const controller = new AbortController();
    abort = controller;
    state.value = "connecting";

    const userHandler = handlerFactory();
    const handler: SseHandler = {
      onEvent: (ev, data) => {
        if (state.value === "connecting") {
          state.value = "open";
        }
        userHandler.onEvent?.(ev, data);
      },
      onMessage: userHandler.onMessage,
      onProgress: userHandler.onProgress,
      onFrame: userHandler.onFrame,
      onState: userHandler.onState,
      onError: (err) => {
        lastError.value = err;
        state.value = "failed";
        userHandler.onError?.(err);
      },
      onDone: () => {
        state.value = "closed";
        userHandler.onDone?.();
      },
    };

    try {
      const resolvedPath = typeof path === "function" ? path() : path;
      const resolvedWsPath = opts.wsPath
        ? typeof opts.wsPath === "function"
          ? opts.wsPath()
          : opts.wsPath
        : undefined;

      if (resolvedWsPath) {
        // WS-first transport with SSE fallback.
        const wsOpts: WsStreamOptions = {
          signal: controller.signal,
          sseFallbackPath: resolvedPath,
          sseOptions: { ...opts, signal: controller.signal },
          idleTimeoutMs: opts.idleTimeoutMs,
        };
        await apiWsStream(resolvedWsPath, handler, wsOpts);
      } else {
        await apiSSE(resolvedPath, handler, {
          ...opts,
          signal: controller.signal,
        });
      }
      // apiSSE resolved normally — the body either ended on `event: done`
      // (handler set `closed`) or hit body EOF without a terminator.
      // Read through an explicitly-widened cast to avoid TS narrowing
      // `state.value` to its post-assignment literal: `apiSSE`'s callbacks
      // may have already mutated it to `open` / `closed` / `failed`.
      const post = state.value as SseConnectionState;
      if (post !== "closed" && post !== "failed") {
        state.value = "closed";
      }
    } catch (cause) {
      // ApiError already passed to handler.onError; just guard the state.
      const post = state.value as SseConnectionState;
      if (post !== "failed") {
        lastError.value = cause as ApiError;
        state.value = "failed";
      }
    } finally {
      abort = null;
    }
  }

  async function open(): Promise<void> {
    if (state.value === "connecting" || state.value === "open") return;
    clearScheduledRetry();
    attempt = 0;

    // Loop with backoff. We want each `open()` call to be awaitable for
    // the *first* attempt only — subsequent retries fire-and-forget so
    // the consumer's `await open()` resolves promptly after the initial
    // success/failure.
    await runOnce();
    while (state.value === "failed" && attempt < retries.length) {
      const delay = retries[attempt];
      attempt += 1;
      // Schedule the next attempt without blocking the consumer.
      scheduledRetry = setTimeout(() => {
        scheduledRetry = null;
        // Fire-and-forget; further failures will keep scheduling until
        // retries run out or `close()` is called.
        void runOnce();
      }, delay);
      // Break out — caller's `await open()` gets back the first failure
      // immediately so they can render an error UI without blocking on
      // the async backoff chain.
      break;
    }
  }

  function close(): void {
    clearScheduledRetry();
    if (abort !== null) {
      abort.abort();
      abort = null;
    }
    if (state.value !== "failed") {
      state.value = "closed";
    }
  }

  // Auto-tear-down on unmount.
  onScopeDispose(() => {
    close();
  });

  return { state, lastError, open, close };
}
