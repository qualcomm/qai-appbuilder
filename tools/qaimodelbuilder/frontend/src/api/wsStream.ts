// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * WebSocket-first streaming with SSE fallback — `apiWsStream`.
 *
 * Opens a WebSocket to the given path. If the handshake fails or the
 * connection is rejected (onerror fires BEFORE any onmessage), transparently
 * falls back to `apiSSE` on the corresponding SSE path.
 *
 * The WS wire protocol (server → client):
 *   {"type": "frame", "event": "<name>", "payload": {...}}
 *   {"type": "done"}
 *   {"type": "error", "code": "...", "message": "..."}
 *   {"type": "__heartbeat__"}
 *
 * Client → Server (optional):
 *   First message after open = request body for request-initiated streams
 *   (set via `opts.initMessage`).
 *
 * Consumers pass the same `SseHandler` used by `apiSSE`, so switching
 * between transports is transparent to callers.
 */

import { apiSSE, type SseHandler, type SseOptions } from "./sse";
import { wsBaseUrl } from "./base";
import { ApiError } from "./errors";

export interface WsStreamOptions {
  /** Per-request AbortSignal — aborts both WS and fallback SSE. */
  signal?: AbortSignal;
  /** SSE fallback path (defaults to wsPath without trailing `/ws`). */
  sseFallbackPath?: string;
  /** SSE fallback options (passed directly to `apiSSE`). */
  sseOptions?: SseOptions;
  /** Initial message to send after WS open (for request-initiated streams). */
  initMessage?: unknown;
  /** Idle timeout in ms (default 120_000). Resets on any message. */
  idleTimeoutMs?: number;
}

/** Build an absolute WS URL from the given path. */
function buildWsUrl(wsPath: string): string {
  const base = wsBaseUrl();
  if (base === "") {
    return wsPath.startsWith("/") ? wsPath : `/${wsPath}`;
  }
  return wsPath.startsWith("/") ? `${base}${wsPath}` : `${base}/${wsPath}`;
}

/** Derive the SSE fallback path when not explicitly provided. */
function deriveSseFallbackPath(wsPath: string): string {
  // Strip trailing `/ws` to get the SSE equivalent.
  if (wsPath.endsWith("/ws")) {
    return wsPath.slice(0, -3) + "/stream";
  }
  return wsPath;
}

/**
 * Open a WS stream, deliver events to handler. Falls back to SSE on failure.
 *
 * Returns a promise that resolves when the stream terminates normally
 * (`done` frame, WS close after messages, or abort) and rejects if the
 * stream produced an error frame or a mid-stream WS failure.
 */
export async function apiWsStream(
  wsPath: string,
  handler: SseHandler,
  opts: WsStreamOptions = {},
): Promise<void> {
  console.debug("[wsStream] attempting WS:", wsPath);
  const idleTimeoutMs = opts.idleTimeoutMs ?? 120_000;
  const idleEnabled = Number.isFinite(idleTimeoutMs) && idleTimeoutMs > 0;

  // If WebSocket is not available (SSR / non-browser), go straight to SSE.
  if (typeof WebSocket === "undefined") {
    return fallbackToSse(wsPath, handler, opts);
  }

  // Abort already fired — bail immediately.
  if (opts.signal?.aborted) {
    return;
  }

  const url = buildWsUrl(wsPath);

  let ws: WebSocket;
  try {
    ws = new WebSocket(url);
  } catch {
    // Constructor threw (bad URL, etc.) — fall back to SSE.
    return fallbackToSse(wsPath, handler, opts);
  }

  return new Promise<void>((resolve, reject) => {
    let receivedAnyMessage = false;
    let terminated = false;
    let idleTimer: ReturnType<typeof setTimeout> | undefined;

    /** Reset idle watchdog. */
    function resetIdle(): void {
      if (!idleEnabled) return;
      if (idleTimer !== undefined) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => {
        idleTimer = undefined;
        cleanup();
        reject(
           new ApiError(
            {
              type: "StreamIdleTimeoutError",
              code: "client.stream_idle_timeout",
              message: `WS stream idle for ${idleTimeoutMs}ms with no data received.`,
            },
            0,
          ),
        );
      }, idleTimeoutMs);
    }

    /** Clear idle timer. */
    function clearIdle(): void {
      if (idleTimer !== undefined) {
        clearTimeout(idleTimer);
        idleTimer = undefined;
      }
    }

    /** Close the WebSocket and clear timers. */
    function cleanup(): void {
      clearIdle();
      terminated = true;
      try {
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
          ws.close();
        }
      } catch {
        // ignore
      }
    }

    /** Handle abort signal. */
    function onAbort(): void {
      if (terminated) return;
      cleanup();
      resolve();
    }

    if (opts.signal) {
      opts.signal.addEventListener("abort", onAbort, { once: true });
    }

    ws.onopen = () => {
      console.debug("[wsStream] WS connected:", wsPath);
      // Send initMessage if provided.
      if (opts.initMessage !== undefined) {
        try {
          ws.send(JSON.stringify(opts.initMessage));
        } catch {
          // send failure — treat as connection failure → fallback.
          cleanup();
          if (opts.signal) opts.signal.removeEventListener("abort", onAbort);
          fallbackToSse(wsPath, handler, opts).then(resolve, reject);
          return;
        }
      }
      resetIdle();
    };

    ws.onmessage = (ev: MessageEvent) => {
      if (terminated) return;
      receivedAnyMessage = true;
      resetIdle();

      let msg: { type: string; event?: string; payload?: unknown; code?: string; message?: string; details?: Record<string, unknown> };
      try {
        msg = JSON.parse(ev.data as string);
      } catch {
        // Unparseable message — pass raw data to onEvent (matches apiSSE behavior).
        handler.onEvent?.("message", { _raw: ev.data });
        return;
      }

      if (msg.type === "__heartbeat__") {
        // Just resets idle timer (already done above).
        return;
      }

      if (msg.type === "done") {
        handler.onDone?.();
        cleanup();
        if (opts.signal) opts.signal.removeEventListener("abort", onAbort);
        resolve();
        return;
      }

      if (msg.type === "error") {
        const err = new ApiError(
          {
            type: msg.code ?? "StreamError",
            code: msg.code ?? "ws.stream_error",
            message: msg.message ?? "WebSocket stream error",
            details: msg.details,
          },
          0,
        );
        handler.onError?.(err);
        cleanup();
        if (opts.signal) opts.signal.removeEventListener("abort", onAbort);
        reject(err);
        return;
      }

      if (msg.type === "frame") {
        const event = msg.event ?? "";
        const payload = msg.payload;

        // Dispatch to typed handlers matching SseHandler interface.
        handler.onEvent?.(event, payload);
        switch (event) {
          case "state":
            handler.onState?.(payload);
            break;
          case "progress":
            handler.onProgress?.(payload);
            break;
          case "frame":
            handler.onFrame?.(payload);
            break;
          case "message":
            handler.onMessage?.(payload);
            break;
          default:
            // Unknown event names — onEvent already called above.
            break;
        }
      }
    };

    ws.onerror = () => {
      if (terminated) return;
      // If we never received any message, the handshake failed → fallback.
      if (!receivedAnyMessage) {
        console.debug("[wsStream] WS failed, falling back to SSE:", opts.sseFallbackPath ?? deriveSseFallbackPath(wsPath));
        cleanup();
        if (opts.signal) opts.signal.removeEventListener("abort", onAbort);
        fallbackToSse(wsPath, handler, opts).then(resolve, reject);
        return;
      }
      // Mid-stream error — will be followed by onclose.
    };

    ws.onclose = (ev: CloseEvent) => {
      if (terminated) return;
      console.debug("[wsStream] WS closed: code=%d reason=%s path=%s", ev.code, ev.reason, wsPath);
      clearIdle();
      terminated = true;
      if (opts.signal) opts.signal.removeEventListener("abort", onAbort);

      if (!receivedAnyMessage) {
        // Never got a message — fallback to SSE.
        fallbackToSse(wsPath, handler, opts).then(resolve, reject);
        return;
      }

      // If we received messages and the close was clean (1000 / 1001),
      // treat as graceful end (same as body EOF in apiSSE).
      if (ev.code === 1000 || ev.code === 1001) {
        resolve();
        return;
      }

      // Unexpected close mid-stream — real error.
      reject(
        new ApiError(
          {
            type: "WsClosedError",
            code: "client.ws_closed",
            message: `WebSocket closed unexpectedly (code ${ev.code}: ${ev.reason || "no reason"}).`,
          },
          0,
        ),
      );
    };
  });
}

/** Fall back to SSE transport. */
function fallbackToSse(
  wsPath: string,
  handler: SseHandler,
  opts: WsStreamOptions,
): Promise<void> {
  const ssePath = opts.sseFallbackPath ?? deriveSseFallbackPath(wsPath);
  const sseOpts: SseOptions = {
    signal: opts.signal,
    idleTimeoutMs: opts.idleTimeoutMs,
    ...opts.sseOptions,
  };
  return apiSSE(ssePath, handler, sseOpts);
}
