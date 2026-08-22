// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Harness control tool frame parsers (V2 enhancement): `todowrite` + `question`.
 *
 * Extracted from `frameHandlers.ts` (cohesion: keep the per-frame reducer file
 * focused on the V1-parity stream machine). Both functions parse a
 * `tool_call` frame's `arguments` into the dedicated tab UI state and apply it
 * via the same `FrameHandlerContext.patchTab` the reducers use — they never
 * touch Pinia directly. `handleToolCall` calls these in addition to rendering
 * the normal tool card, so `todowrite` drives the task list surfaces (top
 * bar + in-conversation card) and `question` activates the in-conversation
 * ChatQuestionCard (the answer is composed front-end into one round-trippable
 * string — see `composeAnswer` / `parseAnswer`).
 */
import type { FrameHandlerContext } from "./frameHandlers";
import type {
  ChatMessage,
  ChatTab,
  PendingQuestion,
  PendingQuestionItem,
  PendingQuestionOption,
  TodoItem,
} from "../_chatTabsTypes";

const VALID_TODO_STATUS = new Set([
  "pending",
  "in_progress",
  "completed",
  "cancelled",
]);

/** Parse + sanitise a `todowrite` call's `arguments.todos` into `TodoItem[]`.
 *  Shared by the live frame handler and the history extractor so the live
 *  and reloaded task lists are byte-identical. Invalid entries are skipped;
 *  a non-array / empty payload yields `[]`. */
export function parseTodos(args: Record<string, unknown>): TodoItem[] {
  const raw = args["todos"];
  const out: TodoItem[] = [];
  if (!Array.isArray(raw)) return out;
  for (const item of raw) {
    if (item === null || typeof item !== "object") continue;
    const obj = item as Record<string, unknown>;
    const content = obj["content"];
    const status = obj["status"];
    if (typeof content !== "string" || content.trim() === "") continue;
    if (typeof status !== "string" || !VALID_TODO_STATUS.has(status)) continue;
    const priority = obj["priority"];
    out.push({
      content: content.trim(),
      status: status as TodoItem["status"],
      ...(priority === "high" || priority === "medium" || priority === "low"
        ? { priority }
        : {}),
    });
  }
  return out;
}

/** Replace `tab.todoList` from a `todowrite` call's `arguments.todos`.
 *  The model sends the COMPLETE list every time, so this is a wholesale
 *  replace (not a merge). Invalid entries are skipped defensively; an
 *  all-invalid / empty payload clears the list (panel hides). */
export function applyTodoWrite(
  args: Record<string, unknown>,
  ctx: FrameHandlerContext,
): void {
  ctx.patchTab({ todoList: parseTodos(args) });
}

/** Extract the LATEST task list from a (history-rehydrated) message list.
 *
 *  The top TaskListBar reads `tab.todoList`, which is only filled live by
 *  `applyTodoWrite`. On a history reload the store rehydrates `messages`
 *  (incl. each `todowrite` tool call's persisted `args`) but never sets
 *  `tab.todoList`, so the bar would stay hidden after a reload. This walks
 *  the messages newest-first and returns the todos of the most recent
 *  `todowrite` tool call (mirroring the live "last write wins" semantics).
 *  Returns `[]` when no todowrite call exists. */
export function extractLatestTodoList(
  messages: ReadonlyArray<ChatMessage>,
): TodoItem[] {
  for (let i = messages.length - 1; i >= 0; i--) {
    const calls = messages[i]?.toolCalls;
    if (calls === undefined) continue;
    // A single message may carry several tool calls; the last todowrite in
    // the newest message wins (consistent with live frame ordering).
    for (let j = calls.length - 1; j >= 0; j--) {
      const call = calls[j];
      if (call?.tool === "todowrite") {
        return parseTodos(call.args);
      }
    }
  }
  return [];
}

/** Parse the `options` array of one question into `PendingQuestionOption[]`.
 *  Invalid / unlabelled entries are skipped defensively. */
function parseQuestionOptions(raw: unknown): PendingQuestionOption[] {
  const options: PendingQuestionOption[] = [];
  if (!Array.isArray(raw)) return options;
  for (const opt of raw) {
    if (opt === null || typeof opt !== "object") continue;
    const o = opt as Record<string, unknown>;
    const label = o["label"];
    if (typeof label !== "string" || label === "") continue;
    options.push({
      label,
      ...(typeof o["description"] === "string"
        ? { description: o["description"] as string }
        : {}),
    });
  }
  return options;
}

/** Parse one raw question object (`{question, header?, options?, multiple?}`)
 *  into a `PendingQuestionItem`. Returns `null` when `question` is missing /
 *  blank (the entry is dropped). */
function parseQuestionItem(obj: Record<string, unknown>): PendingQuestionItem | null {
  const question = obj["question"];
  if (typeof question !== "string" || question.trim() === "") return null;
  const header =
    typeof obj["header"] === "string" && obj["header"] !== ""
      ? (obj["header"] as string)
      : undefined;
  return {
    question: question.trim(),
    ...(header !== undefined ? { header } : {}),
    options: parseQuestionOptions(obj["options"]),
    multiple: obj["multiple"] === true,
  };
}

/** Parse a `question` tool call's `arguments` into the normalised
 *  `PendingQuestionItem[]` the card consumes.
 *
 *  Two wire shapes are accepted (AGENTS.md §3.4 — backend is the wire truth):
 *    - NEW (preferred): `arguments.questions = [{question, header?, options?,
 *      multiple?}, …]` — a multi-question batch.
 *    - LEGACY (back-compat): top-level `arguments.{question, header?, options?,
 *      multiple?}` — a single question, wrapped into a one-element array.
 *  Shared by the live frame handler and the history extractor so the live and
 *  reloaded cards are identical. Returns `[]` when nothing valid is found. */
export function parseQuestions(
  args: Record<string, unknown>,
): PendingQuestionItem[] {
  const raw = args["questions"];
  if (Array.isArray(raw)) {
    const out: PendingQuestionItem[] = [];
    for (const item of raw) {
      if (item === null || typeof item !== "object") continue;
      const parsed = parseQuestionItem(item as Record<string, unknown>);
      if (parsed !== null) out.push(parsed);
    }
    return out;
  }
  // Legacy single-question shape.
  const single = parseQuestionItem(args);
  return single !== null ? [single] : [];
}

/** Strip the fixed `The user answered: ` prefix the backend
 *  (`QuestionToolHandler.execute` in
 *  `src/qai/chat/adapters/harness_tools.py`) wraps around the user's composed
 *  answer when feeding the tool RESULT back to the model.
 *
 *  The read-only ChatQuestionCard renders from that tool result, so the
 *  prefix MUST be removed before line-parsing — otherwise the first
 *  question's line starts with `The user answered: Q1 (...)` instead of
 *  `Q1 (...)`, fails the `^Q\d+` match in `parseAnswer`, and that question's
 *  answer + image attachments get lost / misattributed. Centralised here so
 *  there is exactly one place that knows the wire prefix. */
const ANSWER_TOOL_RESULT_PREFIX_RE = /^The user answered:\s*/;
export function stripAnswerToolResultPrefix(toolResult: string): string {
  return toolResult.replace(ANSWER_TOOL_RESULT_PREFIX_RE, "");
}

/** Raise an in-conversation question card from a `question` call's `arguments`.
 *  Sets `tab.pendingQuestion` (the "active / awaiting answer" pointer that the
 *  ChatQuestionCard rendered for this frame reads). Idempotent on the frame id
 *  so a re-delivered frame does not re-activate an already-answered card. */
export function applyQuestion(
  tab: ChatTab,
  frameId: string,
  args: Record<string, unknown>,
  ctx: FrameHandlerContext,
): void {
  // Skip if this exact question frame is already active (re-delivery guard).
  if (tab.pendingQuestion !== null && tab.pendingQuestion.frameId === frameId) {
    return;
  }
  const questions = parseQuestions(args);
  if (questions.length === 0) return;
  const pending: PendingQuestion = { frameId, questions };
  ctx.patchTab({ pendingQuestion: pending });
}

// ── Answer string compose / parse (V2 enhancement) ─────────────────────────
// The backend feeds the user's answer back to the model VERBATIM as a single
// string (POST /api/chat/answer `answer` is one string; the server does not
// parse its internal structure). So multi-question answers must be composed
// front-end into one readable, ROUND-TRIPPABLE string, and parsed back when
// rebuilding the read-only (answered) card from history.
//
// Format (one line per question):
//   Q1 (问题文本): 答案
//   Q2 (问题文本): 答案
// A single, option-less question collapses to just the bare answer (no `Qn`
// prefix) so the model sees a clean answer for the common 1-question case.

const ANSWER_LINE_RE = /^Q(\d+)\s*\(([^)]*)\):\s?([\s\S]*)$/;

/** Compose the per-question answers into the single string POSTed back.
 *  `answers[i]` is the already-joined answer for `questions[i]` (selected
 *  option labels + any custom text, joined by ", "). */
export function composeAnswer(
  questions: ReadonlyArray<PendingQuestionItem>,
  answers: ReadonlyArray<string>,
): string {
  if (questions.length <= 1) {
    return (answers[0] ?? "").trim();
  }
  const lines: string[] = [];
  for (let i = 0; i < questions.length; i++) {
    const q = questions[i];
    if (q === undefined) continue;
    lines.push(`Q${i + 1} (${q.question}): ${(answers[i] ?? "").trim()}`);
  }
  return lines.join("\n");
}

/** Parse a composed answer string back into per-question answer text, indexed
 *  to match `questions`. Mirrors `composeAnswer` so a history-reloaded card
 *  shows exactly what the user submitted. Falls back to treating the whole
 *  string as question 0's answer when it does not match the multi-question
 *  format (legacy / single-question answers).
 *
 *  A single question's answer MAY span multiple lines (a multi-line custom
 *  answer, or image markdown which `uploadPendingImages` appends on its own
 *  line). So we scan line-by-line: a line that matches `Qn (...)` opens a new
 *  question segment (its answer starts after the colon); every following line
 *  that does NOT open a new segment is appended (with `\n`) to the current
 *  segment. This keeps the image/markdown lines attached to the right
 *  question instead of silently dropping them. */
export function parseAnswer(
  questions: ReadonlyArray<PendingQuestionItem>,
  answer: string,
): string[] {
  const out: string[] = questions.map(() => "");
  if (questions.length <= 1) {
    out[0] = answer.trim();
    return out;
  }
  const segments: string[][] = out.map(() => []);
  let current = -1;
  let matchedAny = false;
  for (const line of answer.split("\n")) {
    const m = ANSWER_LINE_RE.exec(line);
    if (m !== null) {
      const idx = Number.parseInt(m[1] ?? "", 10) - 1;
      if (Number.isInteger(idx) && idx >= 0 && idx < out.length) {
        current = idx;
        segments[current]?.push(m[3] ?? "");
        matchedAny = true;
        continue;
      }
    }
    // Continuation line of the current question's answer (multi-line / image).
    if (current >= 0) segments[current]?.push(line);
  }
  if (!matchedAny) {
    out[0] = answer.trim();
    return out;
  }
  for (let i = 0; i < out.length; i++) {
    out[i] = (segments[i] ?? []).join("\n").trim();
  }
  return out;
}
