// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Coding tool name → icon map (V1 parity, AiCodingPanel.js getToolIcon helper).
 *
 * V1 used a precise per-tool mapping (~13 entries) rather than substring
 * heuristics. Substring matching (`name.includes("read")`) was prone to
 * false positives — e.g. a hypothetical "ReadOnlyTask" tool would render
 * as 📖 instead of 🎯. Keeping a literal map matches V1 visuals 1:1 and
 * makes adding new tools an explicit, reviewable change.
 *
 * Used by both `CodingPermissionDialog.vue` (real-time approval card) and
 * `CodingToolCallCard.vue` (per-message tool-call rows). Unknown tools
 * fall back to 🛠️ (V1 default emoji).
 */

/** Default fallback icon for any tool not in {@link TOOL_ICON_MAP}. */
export const DEFAULT_TOOL_ICON = "🛠️";

/**
 * V1-parity tool icon registry. Keys are the canonical tool names emitted
 * by the Claude Code SDK / Open Code provider on the wire (case-sensitive
 * for the primary entry, case-insensitive lookup via {@link iconForTool}).
 */
export const TOOL_ICON_MAP: Readonly<Record<string, string>> = Object.freeze({
  Read: "📖",
  Write: "✏️",
  Edit: "✏️",
  MultiEdit: "✏️",
  Bash: "⌨️",
  Grep: "🔍",
  Glob: "📁",
  LS: "📂",
  WebFetch: "🌐",
  WebSearch: "🔎",
  Task: "🎯",
  TodoWrite: "☑️",
  BackgroundProcess: "🚀",
  NotebookEdit: "📓",
});

/**
 * Resolve a tool name to its display icon.
 *
 * Lookup order (V1 parity):
 *   1. exact match against {@link TOOL_ICON_MAP}
 *   2. case-insensitive match against the same keys (handles SDK casing
 *      drift like `read` vs `Read`)
 *   3. {@link DEFAULT_TOOL_ICON} fallback
 */
export function iconForTool(tool: string): string {
  if (tool === "") return DEFAULT_TOOL_ICON;
  const direct = TOOL_ICON_MAP[tool];
  if (direct !== undefined) return direct;
  const lower = tool.toLowerCase();
  for (const [key, icon] of Object.entries(TOOL_ICON_MAP)) {
    if (key.toLowerCase() === lower) return icon;
  }
  return DEFAULT_TOOL_ICON;
}
