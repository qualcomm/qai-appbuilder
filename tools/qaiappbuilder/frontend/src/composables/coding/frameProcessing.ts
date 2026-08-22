// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `coding/frameProcessing.ts` — pure SSE-frame processing for the Claude Code
 * (`cc`) / Open Code (`oc`) chat working surfaces (功能块 6 + 7).
 *
 * This module factors the V2 ai_coding SSE wire handling out of
 * `useCodingSession.ts`'s former monolithic `sendMessage` (内聚度纠偏 — pure
 * refactor, behavior unchanged). Every function here is a **pure** transform
 * over the data it is handed: the per-frame `apply*` handlers mutate only the
 * `assistant` `CodingMessage` they receive (and, for error/permission frames,
 * call back through an explicit `FrameContext` for the two cross-cutting side
 * effects — pending-permission and per-session progress). There is no
 * module-level mutable state, so each handler is independently unit-testable.
 *
 * The SSE frame contract is V2's `event: message\ndata:
 * {kind,sequence,payload}` with
 * `kind ∈ {text, tool_call, tool_result, permission_request, error,
 *          task_started, task_progress, task_notification}`.
 * `end → event: done`, `error → event: error` are handled by the SSE
 * transport's onDone/onError callbacks in `useCodingSession.ts`, not here.
 *
 * The `format*` / `frame*` helpers below were moved verbatim from
 * `useCodingSession.ts` (V1 claude-code-utils.js parity is preserved); the
 * `apply*` handlers carry the exact logic of the former `onMessage` switch
 * cases.
 */
import type {
  CodingMessage,
  CodingSubTask,
  CodingToolCall,
  PermissionRequest,
  SessionProgress,
} from "../useCodingSession";

/** A minimal i18n translate signature (vue-i18n's `t`). */
export type Translator = (key: string, named?: Record<string, unknown>) => string;

/**
 * Cross-cutting side effects the frame handlers need beyond mutating
 * `assistant`. Kept as an explicit injected context (no global mutable
 * state) so the handlers stay pure and testable: the caller wires these to
 * the per-kind reactive state in `useCodingSession.ts`.
 */
export interface FrameContext {
  /** i18n translator (V1 error / tool-use copy). */
  t: Translator;
  /** The session the current stream belongs to. */
  sessionId: string;
  /** Set/clear the pending permission request (V1 pendingPermission). */
  setPendingPermission(pr: PermissionRequest | null): void;
  /** Current pending permission's session id, for error-frame clearing. */
  pendingPermissionSessionId(): string | null;
  /** Set/clear the per-session progress indicator (V1 sessionProgress). */
  setSessionProgress(sessionId: string, progress: SessionProgress | null): void;
  /** Stable id generator (crypto.randomUUID wrapper). */
  newId(): string;
}

// ─── Frame payload extractors (V2 authoritative — 阶段0 capture) ────────────────

export function asRecord(v: unknown): Record<string, unknown> {
  return v !== null && typeof v === "object" ? (v as Record<string, unknown>) : {};
}

export function frameText(payload: Record<string, unknown>): string {
  const t = payload["text"] ?? payload["delta"];
  return typeof t === "string" ? t : "";
}

export function frameToolCall(
  payload: Record<string, unknown>,
  newId: () => string,
): CodingToolCall {
  const id =
    (payload["id"] as string | undefined) ??
    (payload["tool_use_id"] as string | undefined) ??
    (payload["call_id"] as string | undefined) ??
    newId();
  const tool =
    (payload["tool"] as string | undefined) ??
    (payload["name"] as string | undefined) ??
    (payload["tool_name"] as string | undefined) ??
    "tool";
  const rawArgs = payload["args"] ?? payload["input"] ?? {};
  return {
    id,
    tool,
    args: asRecord(rawArgs),
    status: "running",
  };
}

export function frameToolResultKey(payload: Record<string, unknown>): string | null {
  const k =
    payload["tool_use_id"] ??
    payload["call_id"] ??
    payload["id"] ??
    payload["tool_call_id"];
  return typeof k === "string" ? k : null;
}

export function frameToolResultText(payload: Record<string, unknown>): string {
  const c = payload["content"] ?? payload["output"] ?? payload["result"];
  if (typeof c === "string") return c;
  if (c !== undefined && c !== null) {
    try {
      return JSON.stringify(c);
    } catch {
      return String(c);
    }
  }
  return "";
}

export function frameToolResultIsError(payload: Record<string, unknown>): boolean {
  return payload["is_error"] === true || payload["isError"] === true;
}

export function framePermission(
  payload: Record<string, unknown>,
  sessionId: string,
): PermissionRequest | null {
  const reqId =
    (payload["request_id"] as string | undefined) ??
    (payload["id"] as string | undefined);
  if (typeof reqId !== "string" || reqId === "") return null;
  const tool =
    (payload["tool"] as string | undefined) ??
    (payload["tool_name"] as string | undefined) ??
    "tool";
  const args = asRecord(payload["args"] ?? payload["input"]);
  return { request_id: reqId, tool, args, sessionId };
}

// ─── V1-parity copy formatters ─────────────────────────────────────────────────

/**
 * Error code → user-facing message (V1 claude-code-utils.js:196-214
 * `formatErrorMessage`). Falls back to the backend `message`, then a generic
 * "unknown error". `initialize_timeout` / `cli_crash` prefer the backend
 * message when present (V1 lines 210-212).
 */
export function formatErrorMessage(
  t: Translator,
  code: string | undefined,
  message: string | undefined,
): string {
  const codeMessages: Record<string, string> = {
    session_not_found: t("claudeCode.errSessionNotFound"),
    session_closed: t("claudeCode.errSessionClosed"),
    session_busy: t("claudeCode.errSessionBusy"),
    timeout: t("claudeCode.errTimeout"),
    sdk_unavailable: t("claudeCode.errSdkUnavailable"),
    auth_not_configured: t("claudeCode.errAuthNotConfigured"),
    // V2 backend surfaces the unconfigured-provider error with this unified
    // QaiError code (ai_coding.provider_not_available); map it to the same
    // "auth not configured" copy as V1's auth_not_configured (same meaning).
    "ai_coding.provider_not_available": t("claudeCode.errAuthNotConfigured"),
    internal_error: t("claudeCode.errInternal"),
    stream_error: t("claudeCode.errStream"),
    initialize_timeout: t("claudeCode.errInitTimeout"),
    cli_crash: t("claudeCode.errCliCrash"),
  };
  if (
    (code === "initialize_timeout" || code === "cli_crash") &&
    typeof message === "string" &&
    message !== ""
  ) {
    return message;
  }
  if (code !== undefined && codeMessages[code] !== undefined) {
    return codeMessages[code];
  }
  return message !== undefined && message !== ""
    ? message
    : t("claudeCode.errUnknown");
}

/**
 * Tool-use one-liner (V1 claude-code-utils.js:46-69 `formatToolUse`).
 * Mirrors V1's per-tool i18n labels so tool-call cards read identically.
 */
export function formatToolUse(
  t: Translator,
  tool: string,
  input: Record<string, unknown>,
): string {
  const path = (input["file_path"] as string | undefined) ?? "";
  switch (tool) {
    case "Read":
      return t("claudeCode.toolReadFile", { path });
    case "Write":
      return t("claudeCode.toolWriteFile", { path });
    case "Edit":
    case "MultiEdit":
      return t("claudeCode.toolEditFile", { path });
    case "Glob":
      return t("claudeCode.toolFileSearch", {
        pattern: (input["pattern"] as string | undefined) ?? "",
      });
    case "Grep":
      return t("claudeCode.toolContentSearch", {
        pattern: (input["pattern"] as string | undefined) ?? "",
      });
    case "Bash": {
      const cmd = (input["command"] as string | undefined) ?? "";
      return t("claudeCode.toolExec", {
        cmd: cmd.slice(0, 60),
        ellipsis: cmd.length > 60 ? "…" : "",
      });
    }
    case "WebFetch":
      return t("claudeCode.toolFetchPage", {
        url: (input["url"] as string | undefined) ?? "",
      });
    case "WebSearch":
      return t("claudeCode.toolWebSearch", {
        query: (input["query"] as string | undefined) ?? "",
      });
    default:
      return `🔨 ${tool}`;
  }
}

// ─── Per-frame handlers (former onMessage switch cases — logic verbatim) ────────

/** `text` frame — append the streamed delta to the assistant turn. */
export function applyTextFrame(
  assistant: CodingMessage,
  payload: Record<string, unknown>,
): void {
  assistant.content += frameText(payload);
}

/** `tool_call` frame — push a new running tool call (V1 enrich w/ description). */
export function applyToolCallFrame(
  assistant: CodingMessage,
  payload: Record<string, unknown>,
  t: Translator,
  newId: () => string,
): void {
  const tc = frameToolCall(payload, newId);
  // V1: enrich with a human-readable one-liner description.
  tc.description = formatToolUse(t, tc.tool, tc.args);
  (assistant.toolCalls ??= []).push(tc);
}

/** `tool_result` frame — pair with the matching tool call and finalise it. */
export function applyToolResultFrame(
  assistant: CodingMessage,
  payload: Record<string, unknown>,
): void {
  const key = frameToolResultKey(payload);
  const calls = assistant.toolCalls ?? [];
  const match =
    key !== null
      ? calls.find((c) => c.id === key)
      : // Fallback: last running call.
        [...calls].reverse().find((c) => c.status === "running");
  if (match !== undefined) {
    match.output = frameToolResultText(payload);
    match.isError = frameToolResultIsError(payload);
    match.status = match.isError ? "error" : "done";
  }
}

/** `permission_request` frame — surface the pending request + progress badge. */
export function applyPermissionFrame(
  payload: Record<string, unknown>,
  ctx: FrameContext,
): void {
  const pr = framePermission(payload, ctx.sessionId);
  if (pr !== null) {
    ctx.setPendingPermission(pr);
    ctx.setSessionProgress(ctx.sessionId, {
      stage: "awaiting_approval",
      startTime: Date.now(),
    });
  }
}

/** `error` frame — map code → message, surface inline (V1 :961-994). */
export function applyErrorFrame(
  assistant: CodingMessage,
  payload: Record<string, unknown>,
  ctx: FrameContext,
): void {
  const code = payload["code"] as string | undefined;
  const rawMsg = payload["message"] as string | undefined;
  const mapped = formatErrorMessage(ctx.t, code, rawMsg);
  assistant.content +=
    (assistant.content === "" ? "" : "\n\n") + `⚠️ ${mapped}`;
  assistant.isStreaming = false;
  ctx.setSessionProgress(ctx.sessionId, null);
  if (ctx.pendingPermissionSessionId() === ctx.sessionId) {
    ctx.setPendingPermission(null);
  }
}

/** `task_started` frame — start a sub-task card (V1 :862-878). */
export function applyTaskStartedFrame(
  assistant: CodingMessage,
  payload: Record<string, unknown>,
): void {
  const taskId = (payload["task_id"] as string | undefined) ?? "";
  if (taskId !== "") {
    const subs = (assistant.subTasks ??= []);
    if (!subs.some((s) => s.task_id === taskId)) {
      subs.push({
        task_id: taskId,
        description: (payload["description"] as string | undefined) ?? "",
        status: "running",
        summary: "",
        usage: null,
      });
    }
  }
}

/** `task_progress` frame — update usage / last tool (V1 :879-893). */
export function applyTaskProgressFrame(
  assistant: CodingMessage,
  payload: Record<string, unknown>,
): void {
  const taskId = (payload["task_id"] as string | undefined) ?? "";
  const sub = (assistant.subTasks ?? []).find((s) => s.task_id === taskId);
  if (sub !== undefined) {
    const usage = asRecord(payload["usage"]);
    sub.usage = {
      total_tokens: usage["total_tokens"] as number | undefined,
      tool_uses: usage["tool_uses"] as number | undefined,
      duration_ms: usage["duration_ms"] as number | undefined,
    };
    sub.last_tool_name =
      (payload["last_tool_name"] as string | null | undefined) ??
      sub.last_tool_name;
    const desc = payload["description"] as string | undefined;
    if (desc !== undefined && desc !== "") sub.description = desc;
  }
}

/**
 * `task_notification` frame — finalise; append if unseen (V1 :894-908)
 * (notification can arrive before started on resume).
 */
export function applyTaskNotificationFrame(
  assistant: CodingMessage,
  payload: Record<string, unknown>,
): void {
  const taskId = (payload["task_id"] as string | undefined) ?? "";
  const rawStatus = (payload["status"] as string | undefined) ?? "completed";
  const status: CodingSubTask["status"] =
    rawStatus === "failed" || rawStatus === "stopped" || rawStatus === "running"
      ? rawStatus
      : "completed";
  const usageRec = payload["usage"];
  const usage =
    usageRec !== null && typeof usageRec === "object"
      ? {
          total_tokens: (usageRec as Record<string, unknown>)["total_tokens"] as
            | number
            | undefined,
          tool_uses: (usageRec as Record<string, unknown>)["tool_uses"] as
            | number
            | undefined,
          duration_ms: (usageRec as Record<string, unknown>)["duration_ms"] as
            | number
            | undefined,
        }
      : null;
  const summary = (payload["summary"] as string | undefined) ?? "";
  const subs = (assistant.subTasks ??= []);
  const sub = subs.find((s) => s.task_id === taskId);
  if (sub !== undefined) {
    sub.status = status;
    sub.summary = summary;
    if (usage !== null) sub.usage = usage;
  } else if (taskId !== "") {
    subs.push({
      task_id: taskId,
      description: "",
      status,
      summary,
      usage,
    });
  }
}

/**
 * Dispatch a single SSE `message` frame to its handler (former `onMessage`
 * switch — routing + behavior verbatim). The assistant turn and the
 * cross-cutting side effects are mutated through `assistant` / `ctx`.
 *
 * V2 ai_coding wire emits `text | tool_call | tool_result |
 * permission_request | error` on `event: message`, plus the Task/Agent
 * frames `task_started | task_progress | task_notification` (surfaced when an
 * upstream / harness injects Task Agent events — the native Anthropic HTTP
 * SSE wire does not emit them today), plus a final `event: done` (handled by
 * the SSE transport's onDone callback). Other V1 frames (`progress` /
 * `interrupted` / `turn_warning`) and the dispatched `done` payload are
 * intentionally not handled here; final-turn book-keeping lives in the
 * caller's onDone.
 */
export function dispatchFrame(
  assistant: CodingMessage,
  frame: { kind?: string; sequence?: number; payload?: unknown },
  ctx: FrameContext,
): void {
  const payload = asRecord(frame.payload);
  switch (frame.kind) {
    case "text":
      applyTextFrame(assistant, payload);
      break;
    case "tool_call":
      applyToolCallFrame(assistant, payload, ctx.t, ctx.newId);
      break;
    case "tool_result":
      applyToolResultFrame(assistant, payload);
      break;
    case "permission_request":
      applyPermissionFrame(payload, ctx);
      break;
    case "error":
      applyErrorFrame(assistant, payload, ctx);
      break;
    case "task_started":
      applyTaskStartedFrame(assistant, payload);
      break;
    case "task_progress":
      applyTaskProgressFrame(assistant, payload);
      break;
    case "task_notification":
      applyTaskNotificationFrame(assistant, payload);
      break;
    default:
      break;
  }
}
