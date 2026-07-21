// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useToolSubtitle — derive tool-card subtitle info from `(toolName, args)`.
 *
 * Tool-card subtitle structure (`[icon] [category] · [contextual
 * description]`). Two orthogonal lookups keep the surface trivially testable
 * and pure:
 *
 *   - `toolMeta(toolName)` → `{ icon, categoryKey }` — the emoji and the i18n
 *     key of the localised category label. Unknown tools fall back to a
 *     generic `🛠️ Tool` pair (`toolCategory.unknown`).
 *
 *   - `subtitleFromToolCall(toolName, args)` → the contextual description
 *     string (path / pattern / url / …). For most tools this is a pure
 *     parameter-formatting operation driven by the tool schema (e.g. `read`
 *     → `args.path`, `grep` → `args.pattern`). A handful of "open-ended"
 *     tools (`exec`, `agent`, `background_process`) let the model supply a
 *     short natural-language label in a dedicated arg field (`description` /
 *     `name`); we prefer that and fall back to the primary argument.
 *     Returns `null` when there is no meaningful subtitle so the caller can
 *     hide the subtitle slot (e.g. `apply_patch`, `list_subagents`, unknown
 *     tools, missing args).
 *
 * The helper is PURE — no Vue reactivity, no i18n calls — so it composes
 * inside any component (the caller runs `t(meta.categoryKey)`) and is
 * trivially unit-testable.
 */

/** Metadata describing how a tool renders in the subtitle slot. */
export interface ToolMeta {
  /** Single-character emoji or short icon identifier. */
  readonly icon: string;
  /** i18n key of the localised category label (defined under `chat.toolCategory.*`). */
  readonly categoryKey: string;
}

/** Fallback meta for tools not in the mapping table below. */
const DEFAULT_META: ToolMeta = {
  icon: "🛠️",
  categoryKey: "chat.toolCategory.unknown",
};

/**
 * Map a tool name to its icon + i18n category key. Unknown tools return a
 * generic `🛠️ Tool` meta so the subtitle slot can still surface the raw tool
 * name via the caller's default rendering.
 */
export function toolMeta(toolName: string): ToolMeta {
  switch (toolName) {
    case "read":
      return { icon: "📖", categoryKey: "chat.toolCategory.read" };
    case "list":
      return { icon: "📂", categoryKey: "chat.toolCategory.list" };
    case "write":
      return { icon: "📝", categoryKey: "chat.toolCategory.write" };
    case "edit":
      return { icon: "✏️", categoryKey: "chat.toolCategory.edit" };
    case "glob":
      return { icon: "🔍", categoryKey: "chat.toolCategory.glob" };
    case "grep":
      return { icon: "🔎", categoryKey: "chat.toolCategory.grep" };
    case "exec":
      return { icon: "🖥️", categoryKey: "chat.toolCategory.exec" };
    case "webfetch":
      return { icon: "🌐", categoryKey: "chat.toolCategory.webfetch" };
    case "web_search":
      return { icon: "🔎", categoryKey: "chat.toolCategory.web_search" };
    case "apply_patch":
      return { icon: "🩹", categoryKey: "chat.toolCategory.apply_patch" };
    case "appbuilder_run":
      return { icon: "🚀", categoryKey: "chat.toolCategory.appbuilder_run" };
    case "agent":
      return { icon: "🤖", categoryKey: "chat.toolCategory.agent" };
    case "background_process":
      return {
        icon: "⚙️",
        categoryKey: "chat.toolCategory.background_process",
      };
    case "list_subagents":
      return { icon: "📋", categoryKey: "chat.toolCategory.list_subagents" };
    case "skill":
      return { icon: "🎯", categoryKey: "chat.toolCategory.skill" };
    case "appbuilder_batch_run":
      return {
        icon: "🚀",
        categoryKey: "chat.toolCategory.appbuilder_batch_run",
      };
    default:
      return DEFAULT_META;
  }
}

/**
 * Derive the contextual subtitle string for a tool call. Returns `null` when
 * no meaningful subtitle can be produced (missing args / no relevant field /
 * unknown tool) so the caller can hide the subtitle slot.
 *
 * Semantic model-supplied fields (`description`, `name`) always take
 * precedence over the raw primary argument: when
 * the model has bothered to summarise what it's about to run, show that
 * summary; otherwise surface the raw argument.
 */
export function subtitleFromToolCall(
  toolName: string,
  args: Record<string, unknown> | null | undefined,
): string | null {
  if (args === null || args === undefined) return null;

  /** Safe string accessor: non-empty string or null. */
  const str = (k: string): string | null => {
    const v = args[k];
    return typeof v === "string" && v.length > 0 ? v : null;
  };
  /** Safe number accessor: finite number or null. */
  const num = (k: string): number | null => {
    const v = args[k];
    return typeof v === "number" && Number.isFinite(v) ? v : null;
  };

  switch (toolName) {
    case "read":
    case "list": {
      const p = str("path");
      if (p === null) return null;
      const off = num("offset");
      const lim = num("limit");
      if (off === null && lim === null) return p;
      // Present as `path offset=X limit=Y`. When only one is supplied, fill
      // the other with a sensible default so users see complete pagination
      // context rather than a half-populated pair.
      return `${p} offset=${off ?? 0} limit=${lim ?? 2000}`;
    }
    case "write":
    case "edit":
      return str("path");
    case "glob":
      return str("pattern");
    case "grep": {
      const pat = str("pattern");
      if (pat === null) return null;
      const p = str("path");
      return p !== null ? `${pat} in ${p}` : pat;
    }
    case "exec": {
      // Prefer the model-supplied description; fall back to the first
      // command line truncated to 60 chars so a huge multi-line script does
      // not blow the header.
      const desc = str("description");
      if (desc !== null) return desc;
      const cmd = str("command");
      if (cmd === null) return null;
      const firstLine = cmd.split("\n")[0] ?? cmd;
      return firstLine.slice(0, 60);
    }
    case "webfetch":
      return str("url");
    case "web_search":
      return str("query");
    case "appbuilder_run":
      return str("modelId");
    case "appbuilder_batch_run": {
      // Same primary field as `appbuilder_run` (all batch items share the
      // one model). Append batch length so the header conveys scale at a
      // glance (e.g. `"yolov5-nano (12 items)"`).
      const modelId = str("modelId");
      if (modelId === null) return null;
      const batch = args["batch"];
      const n = Array.isArray(batch) ? batch.length : null;
      return n !== null ? `${modelId} (${n} items)` : modelId;
    }
    case "apply_patch":
      // No good short field — `patch` is the whole unified diff (already
      // rendered by the diff-preview block below the header).
      return null;
    case "agent": {
      // Model-supplied short tag preferred; otherwise the first 30 chars of
      // the prompt so the card still tells the user what the sub-agent is
      // being asked to do.
      const name = str("name");
      if (name !== null) return name;
      const prompt = str("prompt");
      return prompt !== null ? prompt.slice(0, 30) : null;
    }
    case "background_process":
      return str("description") ?? str("command") ?? str("action");
    case "list_subagents":
      return null;
    case "skill": {
      // `name` is the required skill id (e.g. `model-builder`). Same
      // pagination surface as `read`/`list`: show `offset`/`limit` when
      // either is present so users see the pagination context (default
      // page size is 250 lines per the skill tool schema).
      const name = str("name");
      if (name === null) return null;
      const off = num("offset");
      const lim = num("limit");
      if (off === null && lim === null) return name;
      return `${name} offset=${off ?? 1} limit=${lim ?? 250}`;
    }
    default:
      return null;
  }
}
