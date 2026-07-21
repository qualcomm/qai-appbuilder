// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useSseAiCoding — SSE composable for the ai_coding (Claude Code / OpenCode) context.
 *
 * Per api-contract section 3.4, ai_coding streams emit:
 *
 *   event: message       data: { type, agent, session_id, ... }
 *   event: error         data: ApiErrorPayload
 *   event: done          data: {}
 *   (optional ping comments)
 *
 * Wire format is identical to chat (section 3.1) but the payload
 * discriminator differs; consumers narrow on `type === "delta"` /
 * `"tool_call"` / `"tool_output"` / `"approval_request"` etc. owned by
 * the ai_coding BC.
 */
import { ref, type Ref } from "vue";

import { useSse, type UseSseOptions, type UseSseReturn } from "./useSse";

export interface AiCodingStreamFrame {
  readonly type?: string;
  readonly agent?: string;
  readonly session_id?: string;
  readonly [key: string]: unknown;
}

export interface UseSseAiCodingReturn extends UseSseReturn {
  readonly messages: Ref<readonly AiCodingStreamFrame[]>;
  readonly resetMessages: () => void;
}

export function useSseAiCoding(
  path: string | (() => string),
  opts: UseSseOptions = {},
): UseSseAiCodingReturn {
  const messages: Ref<AiCodingStreamFrame[]> = ref([]);

  const base = useSse(
    path,
    () => ({
      onMessage: (data) => {
        messages.value = [
          ...messages.value,
          (data ?? {}) as AiCodingStreamFrame,
        ];
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
