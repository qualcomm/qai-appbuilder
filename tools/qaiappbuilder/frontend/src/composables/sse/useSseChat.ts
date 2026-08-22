// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useSseChat — SSE composable for the chat context.
 *
 * Per api-contract section 3.1, chat streams emit:
 *
 *   event: message       data: { type, ... }
 *   event: error         data: ApiErrorPayload
 *   event: done          data: {}
 *   (heartbeat comments are silently ignored by apiSSE)
 *
 * Consumers receive `messages` (a reactive append-only array) and the
 * shared connection state. The exact frame discriminator is owned by
 * the chat BC and narrowed at consumption time by SFCs.
 */
import { ref, type Ref } from "vue";

import { useSse, type UseSseOptions, type UseSseReturn } from "./useSse";

export interface ChatStreamFrame {
  readonly type?: string;
  readonly [key: string]: unknown;
}

export interface UseSseChatReturn extends UseSseReturn {
  /** Append-only list of decoded `event: message` frames. */
  readonly messages: Ref<readonly ChatStreamFrame[]>;
  /** Reset the buffer (e.g. before reopening on a new conversation). */
  readonly resetMessages: () => void;
}

/**
 * Open a chat SSE stream against `path` (e.g.
 * `/api/chat/sessions/{id}/stream`). The composable keeps the buffer
 * and shared connection-state refs reactive, so consumers can bind to
 * them in templates without manual unwrapping.
 */
export function useSseChat(
  path: string | (() => string),
  opts: UseSseOptions = {},
): UseSseChatReturn {
  const messages: Ref<ChatStreamFrame[]> = ref([]);

  const base = useSse(
    path,
    () => ({
      onMessage: (data) => {
        messages.value = [...messages.value, (data ?? {}) as ChatStreamFrame];
      },
    }),
    opts,
  );

  function resetMessages(): void {
    messages.value = [];
  }

  return {
    state: base.state,
    lastError: base.lastError,
    open: base.open,
    close: base.close,
    messages,
    resetMessages,
  };
}
