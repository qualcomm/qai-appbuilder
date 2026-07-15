// Front-end equivalents of the back-end mode-policy domain logic.
//
// The back-end `ModeToolPolicy.is_advertised` / `lint_mode` /
// preset-tier detection live in Python (`src/qai/chat/domain/mode_template.py`).
// They are NOT available in the browser, so this module re-implements the SAME
// rules (default `allow`, explicit entry wins; framing-forbids-but-tool-allowed
// soft warning; the 3 tool-policy preset tiers) so the UI can:
//   * grey-out tool chips a selected mode would deny (decision 2);
//   * show lint warnings live as the user edits a mode (decision 10);
//   * detect which preset tier an existing custom mode's tool_policy matches.
//
// Keep this口径-aligned with the Python source: any divergence is a bug.

export type ToolPolicyValue = "allow" | "deny";

export interface ModeToolPolicy {
  default?: ToolPolicyValue;
  tools?: Record<string, ToolPolicyValue>;
}

export interface ModeLintIssue {
  severity: string;
  code: string;
  message: string;
}

/** The 3 named preset tiers + the custom escape hatch (task §3.3 / decision). */
export type PresetTier = "A" | "B" | "C" | "custom";

/**
 * Whether `tool` may be advertised to the speaker's LLM under `policy`.
 *
 * Mirrors `ModeToolPolicy.is_advertised`: no policy = everything advertised
 * (the role's allowed_tools is the only gate); otherwise the explicit
 * per-tool entry wins, falling back to `default` (itself defaulting to
 * `allow`). Returns `true` only when the effective policy is `allow`.
 */
export function isAdvertised(
  policy: ModeToolPolicy | null | undefined,
  tool: string,
): boolean {
  if (!policy) return true;
  const explicit = policy.tools?.[tool];
  const effective = explicit ?? policy.default ?? "allow";
  return effective === "allow";
}

// ---------------------------------------------------------------------------
// Tool-policy preset tiers (must match task §3.3 A/B/C definitions exactly).
// ---------------------------------------------------------------------------
/** A "不限工具": no restriction. */
export const PRESET_A: ModeToolPolicy = { default: "allow", tools: {} };
/** B "只读（不改不执行）": deny write/edit/exec, allow the rest. */
export const PRESET_B: ModeToolPolicy = {
  default: "allow",
  tools: { write: "deny", edit: "deny", exec: "deny" },
};
/** C "只读 + 任务清单": deny by default, allow only read/glob/grep/todowrite. */
export const PRESET_C: ModeToolPolicy = {
  default: "deny",
  tools: { read: "allow", glob: "allow", grep: "allow", todowrite: "allow" },
};

/** Return the canonical policy object for a named preset tier. */
export function presetPolicy(tier: Exclude<PresetTier, "custom">): ModeToolPolicy {
  if (tier === "A") return { default: "allow", tools: {} };
  if (tier === "B")
    return { default: "allow", tools: { write: "deny", edit: "deny", exec: "deny" } };
  return {
    default: "deny",
    tools: { read: "allow", glob: "allow", grep: "allow", todowrite: "allow" },
  };
}

function sameTools(
  a: Record<string, ToolPolicyValue>,
  b: Record<string, ToolPolicyValue>,
): boolean {
  const ak = Object.keys(a);
  const bk = Object.keys(b);
  if (ak.length !== bk.length) return false;
  return ak.every((k) => a[k] === b[k]);
}

/**
 * Identify which preset tier `policy` matches, or `"custom"` when it matches
 * none. A `null`/`undefined` policy is treated as preset A (the permissive
 * default — "不限工具"), so a brand-new / empty mode opens on tier A.
 */
export function detectPresetTier(
  policy: ModeToolPolicy | null | undefined,
): PresetTier {
  const def = policy?.default ?? "allow";
  const tools = policy?.tools ?? {};
  for (const tier of ["A", "B", "C"] as const) {
    const preset = presetPolicy(tier);
    if ((preset.default ?? "allow") === def && sameTools(preset.tools ?? {}, tools)) {
      return tier;
    }
  }
  return "custom";
}

// ---------------------------------------------------------------------------
// lint_mode equivalent (task §7.6 / decision 10 — advisory only, never blocks).
// ---------------------------------------------------------------------------
const _EXECUTION_TOOLS = ["write", "edit", "exec"] as const;
const _FORBIDS_EXEC_TOKENS = [
  "不要执行",
  "不写代码",
  "不要写代码",
  "do not execute",
  "don't execute",
] as const;

/**
 * Detect framing ↔ tool-policy soft conflicts (mirrors back-end `lint_mode`).
 *
 * When the framing prose discourages execution yet an execution tool
 * (`write`/`edit`/`exec`) is still advertised by the tool policy, emit one
 * advisory warning per conflicting tool. The messages mirror the back-end
 * wording (English, tool name embedded — §7.8: lint messages are NOT i18n'd).
 * Pure, advisory only — never blocks saving.
 */
export function lintMode(
  framing: string,
  toolPolicy: ModeToolPolicy | null | undefined,
): ModeLintIssue[] {
  const issues: ModeLintIssue[] = [];
  const forbidsExec = _FORBIDS_EXEC_TOKENS.some((tok) => framing.includes(tok));
  if (!forbidsExec) return issues;
  for (const tool of _EXECUTION_TOOLS) {
    if (isAdvertised(toolPolicy, tool)) {
      issues.push({
        severity: "warning",
        code: "framing_forbids_but_tool_allowed",
        message: `framing discourages execution but tool '${tool}' is still allowed by the tool policy`,
      });
    }
  }
  return issues;
}
