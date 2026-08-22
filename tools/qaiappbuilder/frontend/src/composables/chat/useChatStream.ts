// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useChatStream — SSE/stream wrapper for chat turns.
 *
 * S5 PR-053: minimal end-to-end happy path using the PR-051 `apiSSE`
 * client. Multi-tab WS handling and 4-state machine come in PR-054.
 *
 * Responsibilities:
 *   - Submit a prompt to the chat endpoint.
 *   - Drive `useChatState.appendStreamingChunk` while frames flow.
 *   - Translate `event: error` / `event: done` into terminal states.
 *   - Provide an `abort()` that cancels the in-flight stream.
 */
import { ref, type Ref } from "vue";
import { apiSSE, type ApiError } from "@/api";
import type { ChatStreamFrame } from "@/types/streaming";

export interface UseChatStreamOptions {
  /** Path to the chat SSE endpoint, defaults to `/api/chat/stream`. */
  readonly path?: string;
}

export interface ChatStreamHandlers {
  onChunk?(text: string): void;
  onFrame?(frame: ChatStreamFrame): void;
  onError?(err: ApiError): void;
  onDone?(): void;
}

export interface UseChatStream {
  readonly inFlight: Ref<boolean>;
  readonly lastError: Ref<ApiError | null>;
  send(prompt: string, handlers?: ChatStreamHandlers): Promise<void>;
  abort(): void;
}

export function useChatStream(options: UseChatStreamOptions = {}): UseChatStream {
  const path = options.path ?? "/api/chat/stream";
  const inFlight = ref<boolean>(false);
  const lastError = ref<ApiError | null>(null);
  let controller: AbortController | null = null;

  function abort(): void {
    if (controller !== null) {
      controller.abort();
      controller = null;
    }
    inFlight.value = false;
  }

  async function send(
    prompt: string,
    handlers: ChatStreamHandlers = {},
  ): Promise<void> {
    abort();
    controller = new AbortController();
    inFlight.value = true;
    lastError.value = null;
    try {
      await apiSSE(
        path,
        {
          onMessage(data) {
            const frame = data as ChatStreamFrame<{ text?: string }>;
            const text =
              typeof frame.payload === "object" &&
              frame.payload !== null &&
              "text" in frame.payload &&
              typeof frame.payload.text === "string"
                ? frame.payload.text
                : "";
            if (text !== "") {
              handlers.onChunk?.(text);
            }
            handlers.onFrame?.(frame);
          },
          onError(err) {
            lastError.value = err;
            handlers.onError?.(err);
          },
          onDone() {
            handlers.onDone?.();
          },
        },
        {
          method: "POST",
          body: { prompt },
          signal: controller.signal,
        },
      );
    } finally {
      inFlight.value = false;
      controller = null;
    }
  }

  return {
    inFlight,
    lastError,
    send,
    abort,
  };
}
