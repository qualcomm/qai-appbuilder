// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useChatTools — tool-call UI state composable.
 *
 * S5 PR-053: skeleton + types only. Real wiring of pending →
 * approve/reject flows lands in PR-054 alongside the WS state machine.
 *
 * The legacy useChat.js threaded tool calls through a flat reactive
 * map keyed by `tool_call_id` and emitted user decisions via DOM
 * events. Here we centralise approval state in this composable so
 * callers see a typed view-model.
 */
import { ref, computed, type ComputedRef, type Ref } from "vue";

export type ToolCallStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "running"
  | "completed"
  | "failed";

export interface ToolCall {
  readonly id: string;
  readonly name: string;
  readonly args: Record<string, unknown>;
  readonly status: ToolCallStatus;
  readonly result?: unknown;
  readonly error?: string;
  readonly conversationId?: string;
}

export interface UseChatTools {
  readonly calls: ComputedRef<readonly ToolCall[]>;
  readonly pending: ComputedRef<readonly ToolCall[]>;
  readonly autoApprove: Ref<boolean>;
  recordCall(call: ToolCall): void;
  approve(id: string): void;
  reject(id: string, reason?: string): void;
  markRunning(id: string): void;
  markCompleted(id: string, result: unknown): void;
  markFailed(id: string, error: string): void;
  clear(): void;
}

export function useChatTools(): UseChatTools {
  const internal = ref<ToolCall[]>([]);
  const autoApprove = ref<boolean>(false);

  function patch(id: string, patcher: (call: ToolCall) => ToolCall): void {
    internal.value = internal.value.map((c) => (c.id === id ? patcher(c) : c));
  }

  return {
    calls: computed(() => internal.value),
    pending: computed(() =>
      internal.value.filter((c) => c.status === "pending"),
    ),
    autoApprove,
    recordCall(call) {
      internal.value = [...internal.value, call];
    },
    approve(id) {
      patch(id, (c) => ({ ...c, status: "approved" as ToolCallStatus }));
    },
    reject(id, reason) {
      patch(id, (c) => ({
        ...c,
        status: "rejected" as ToolCallStatus,
        error: reason ?? "rejected",
      }));
    },
    markRunning(id) {
      patch(id, (c) => ({ ...c, status: "running" as ToolCallStatus }));
    },
    markCompleted(id, result) {
      patch(id, (c) => ({
        ...c,
        status: "completed" as ToolCallStatus,
        result,
      }));
    },
    markFailed(id, error) {
      patch(id, (c) => ({ ...c, status: "failed" as ToolCallStatus, error }));
    },
    clear() {
      internal.value = [];
    },
  };
}
