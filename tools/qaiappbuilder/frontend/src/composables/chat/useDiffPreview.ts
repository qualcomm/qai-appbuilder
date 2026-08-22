// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Shared file-change "diff preview" rendering (DISC-1 三期-step6).
 *
 * A SINGLE, mode-agnostic helper used by BOTH the multi-agent implementation
 * panel AND ordinary single-agent chat tool cards (用户 2026-06-24 拍板：做成
 * 共享渲染能力，两模式共用). Given a file-mutating tool's name + arguments it
 * produces a unified-diff-style string (GitHub-PR-like green-add / red-remove)
 * which the caller renders through the existing `renderMarkdown` (a ```diff
 * fenced block → highlight.js diff highlighting). This reuses the project's
 * markdown/highlight infrastructure rather than building a bespoke diff engine
 * (判据 1：复用核心抽象).
 *
 * Supported tools:
 *   - `write`        → `{ path, content }`  → all-added new content.
 *   - `edit`         → `{ path, edits: [{ oldText, newText }] }` → per-edit
 *                      replace blocks rendered as removed/added line groups.
 *   - `apply_patch`  → `{ patch }` / `{ input }` → the patch text IS a unified
 *                      diff, surfaced verbatim.
 *
 * Any other tool (or unusable args) yields `null` → the caller shows no diff.
 * The helper is PURE (no Vue reactivity) so it is trivially unit-testable and
 * can be called from a composable, a component, or a test.
 */
import { renderMarkdown } from "@/composables/markdown";

/** The set of tool names this helper can derive a diff for. */
const DIFF_TOOL_NAMES = new Set(["write", "edit", "apply_patch"]);

/** Cap so a giant write/patch cannot blow the control-plane render (the full
 *  content always lives in the tool result / message system). */
const MAX_DIFF_LINES = 400;

function coerceString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function capLines(lines: string[]): string[] {
  if (lines.length <= MAX_DIFF_LINES) return lines;
  return [
    ...lines.slice(0, MAX_DIFF_LINES),
    `... (${lines.length - MAX_DIFF_LINES} more lines omitted)`,
  ];
}

/** Render a block of text as added (`+`) or removed (`-`) diff lines. */
function prefixLines(text: string, prefix: "+" | "-"): string[] {
  if (text === "") return [];
  return text.replace(/\r\n/g, "\n").split("\n").map((l) => `${prefix}${l}`);
}

/**
 * Derive a unified-diff-style string from a file-mutating tool call.
 * Returns `null` when the tool is not a known mutator or the args are unusable.
 */
export function diffFromToolCall(
  toolName: string,
  args: Record<string, unknown> | null | undefined,
): string | null {
  if (!DIFF_TOOL_NAMES.has(toolName) || args === null || args === undefined) {
    return null;
  }
  const path = coerceString(args.path);

  if (toolName === "apply_patch") {
    // The patch argument already IS a unified diff — surface it verbatim.
    const patch = coerceString(args.patch) || coerceString(args.input);
    if (patch.trim() === "") return null;
    return capLines(patch.replace(/\r\n/g, "\n").split("\n")).join("\n");
  }

  if (toolName === "write") {
    const content = coerceString(args.content);
    if (content === "") return null;
    const header = path !== "" ? [`+++ ${path}`] : [];
    return capLines([...header, ...prefixLines(content, "+")]).join("\n");
  }

  // edit: { path, edits: [{ oldText, newText }] }
  const edits = Array.isArray(args.edits) ? args.edits : [];
  if (edits.length === 0) return null;
  const body: string[] = [];
  for (const raw of edits) {
    if (raw === null || typeof raw !== "object") continue;
    const e = raw as Record<string, unknown>;
    const oldText = coerceString(e.oldText);
    const newText = coerceString(e.newText);
    if (oldText === "" && newText === "") continue;
    body.push("@@ edit @@");
    body.push(...prefixLines(oldText, "-"));
    body.push(...prefixLines(newText, "+"));
  }
  // No usable edit content ⇒ no diff (don't emit a lone header).
  if (body.length === 0) return null;
  const out = path !== "" ? [`--- ${path}`, `+++ ${path}`, ...body] : body;
  return capLines(out).join("\n");
}

/**
 * Render a unified-diff string to sanitised HTML via the shared markdown
 * renderer (wraps it in a ```diff fenced block so highlight.js colours the
 * add/remove lines). Returns `""` for an empty / null diff.
 */
export function renderDiffHtml(diff: string | null | undefined): string {
  if (diff === null || diff === undefined || diff.trim() === "") return "";
  return renderMarkdown("```diff\n" + diff + "\n```");
}

/** Convenience: derive + render in one call. Returns `""` when no diff. */
export function renderToolCallDiff(
  toolName: string,
  args: Record<string, unknown> | null | undefined,
): string {
  return renderDiffHtml(diffFromToolCall(toolName, args));
}

/** Whether a tool name is one this helper can render a diff for (so callers can
 *  decide to show a "changes" affordance without computing the full diff). */
export function isDiffableTool(toolName: string): boolean {
  return DIFF_TOOL_NAMES.has(toolName);
}
