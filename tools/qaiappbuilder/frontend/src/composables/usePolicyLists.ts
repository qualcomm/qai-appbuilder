// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * usePolicyLists — V1 "Allow Lists" 4-category view over the rules-based
 * security Policy.
 *
 * V1 (SecurityConfigPanel.js Tab 2) exposed the policy as four flat string
 * lists — `read_allow` / `write_allow` / `exec_allow_cwd` /
 * `exec_deny_patterns`. V2's backend models the same data as a single
 * `PolicyRule[]` carrying an explicit `op` dimension
 * (`read` / `write` / `exec` / `exec_deny` / `any`). This composable is the
 * adapter: it projects the rules into the four V1 categories for editing and
 * folds edits back into `PolicyRule[]` on save, **preserving any rule that
 * does not belong to one of the four categories** (e.g. legacy `op=any`
 * rules or template rules surfaced on the Overview tab) so this panel never
 * clobbers them.
 *
 * Why a composable (vs V1's giant inline component): the projection,
 * dirty-tracking and fold-back are pure functions of the rule set, so they
 * live here as testable units and keep `SecurityConfigPanel.vue` a thin view.
 */
import { computed, ref } from "vue";

import { apiJson, ApiError } from "@/api";
import type { components } from "@/types/api";

type PolicyResponse = components["schemas"]["PolicyResponse"];
type PolicyRuleDTO = components["schemas"]["_PolicyRuleDTO"];
type PolicyOp = PolicyRuleDTO["op"];

/**
 * The V1 list categories plus the V2 `write_deny` path blocklist, in display
 * order. `write_deny` is ADDITIVE (a `deny` grant on file paths) and does NOT
 * change how the other four categories load/save.
 */
export type ListField =
  | "read_allow"
  | "write_allow"
  | "write_deny"
  | "exec_allow_cwd"
  | "exec_deny_patterns";

export const LIST_FIELDS: readonly ListField[] = [
  "read_allow",
  "write_allow",
  "write_deny",
  "exec_allow_cwd",
  "exec_deny_patterns",
] as const;

/**
 * (op, action) tuple each category folds into when written back to a rule.
 * read/write/exec are `allow` grants; exec_deny is a `deny` regex gate on the
 * command string; write_deny is a `deny` gate on file PATHS (scope=path).
 */
const FIELD_RULE: Record<
  ListField,
  { op: PolicyOp; action: PolicyRuleDTO["action"] }
> = {
  read_allow: { op: "read", action: "allow" },
  write_allow: { op: "write", action: "allow" },
  write_deny: { op: "write", action: "deny" },
  exec_allow_cwd: { op: "exec", action: "allow" },
  exec_deny_patterns: { op: "exec_deny", action: "deny" },
};

/**
 * Return the category a rule belongs to, or null if it is "other".
 *
 * ``write_deny`` (the path blocklist) is special: it captures any DENY rule
 * on file paths whose op is ``write`` OR ``any`` (a bare ``any``+``deny``+
 * ``path`` rule denies writes too), while deliberately NOT capturing
 * ``exec_deny_patterns`` (op=``exec_deny``, a command regex gate — a
 * distinct category) nor ``preset`` / ``user``-scoped deny rules
 * (round-tripped as "other").
 *
 * Symmetrically, ``write_allow`` captures any ALLOW rule on file paths whose
 * op is ``write`` OR ``any`` — an ``op=any`` + ``allow`` rule is the exact
 * shape the pre-2026-07 built-in templates shipped (``tpl-*-allow-project``
 * with ``pattern=${'{'}PROJECT_ROOT{'}'}/*``); before this fold-in they
 * roundtripped as ``null`` and disappeared from the Allow Lists UI entirely,
 * so operators couldn't SEE their own policy — the exact "hidden ALLOW rule"
 * gap that made the strict-template regression invisible. Similarly
 * ``read_allow`` accepts ``op=read`` OR ``op=any`` so an ``op=any`` allow
 * surfaces in both buckets consistently.
 *
 * All other categories match on an exact (op, action) pair as before.
 */
function ruleField(rule: PolicyRuleDTO): ListField | null {
  // Path blocklist: deny on file paths for write/any op (never exec_deny).
  if (
    rule.action === "deny" &&
    rule.scope === "path" &&
    (rule.op === "write" || rule.op === "any")
  ) {
    return "write_deny";
  }
  // Write allow: allow on file paths for write/any op — mirrors the
  // write_deny fold-in above so op=any rules stay visible in the UI.
  if (
    rule.action === "allow" &&
    rule.scope === "path" &&
    (rule.op === "write" || rule.op === "any")
  ) {
    return "write_allow";
  }
  for (const field of LIST_FIELDS) {
    if (field === "write_deny" || field === "write_allow") continue;
    const spec = FIELD_RULE[field];
    if (rule.op === spec.op && rule.action === spec.action) return field;
  }
  return null;
}

type Draft = Record<ListField, string[]>;

function emptyDraft(): Draft {
  return {
    read_allow: [],
    write_allow: [],
    write_deny: [],
    exec_allow_cwd: [],
    exec_deny_patterns: [],
  };
}

function emptyOpMap(): Record<ListField, Record<string, PolicyOp>> {
  return {
    read_allow: {},
    write_allow: {},
    write_deny: {},
    exec_allow_cwd: {},
    exec_deny_patterns: {},
  };
}

export function usePolicyLists() {
  const loading = ref(false);
  const saving = ref(false);
  const readOnly = ref(false);
  const version = ref(0);
  const saveStatus = ref<"" | "success" | "error">("");
  const lastError = ref<string | null>(null);

  /** Editable per-category lists (the working copy the UI binds to). */
  const draft = ref<Draft>(emptyDraft());
  /** Snapshot of the last-loaded draft, for dirty-tracking + reset. */
  const baseline = ref<Draft>(emptyDraft());
  /**
   * Rules that don't belong to any of the four categories — round-tripped
   * untouched so saving the Allow-Lists tab never drops Overview/template
   * rules that are shaped in ways the flat category model can't represent.
   * Non-empty when a policy carries rules the classifier tagged ``null``
   * (e.g. ``preset``/``user``-scoped rules).
   */
  const otherRules = ref<PolicyRuleDTO[]>([]);
  /**
   * Original ``op`` for each classified pattern, keyed by
   * ``${'`${field}::${pattern}`'}``. Used by :func:`foldToRules` to
   * preserve the exact ``op`` of an ``op=any`` rule surfaced in
   * ``write_allow``/``write_deny`` — without this, a save would silently
   * narrow it to ``op=write``, changing the rule's semantics (write
   * implies read, but ``any`` also covers execute — the ``op=any``
   * ``${'{'}PROJECT_ROOT{'}'}/*`` allow rule shipped by the pre-2026-07
   * built-in templates was the canonical example of this shape). The
   * shape is: ``field → pattern → op``.
   */
  const originalOpByField = ref<
    Record<ListField, Record<string, PolicyOp>>
  >(emptyOpMap());

  function projectFromRules(rules: PolicyRuleDTO[]): void {
    const next = emptyDraft();
    const others: PolicyRuleDTO[] = [];
    const opMap = emptyOpMap();
    for (const rule of rules) {
      const field = ruleField(rule);
      if (field) {
        next[field].push(rule.pattern);
        opMap[field][rule.pattern] = rule.op;
      } else {
        others.push(rule);
      }
    }
    draft.value = next;
    baseline.value = structuredClone(next);
    otherRules.value = others;
    originalOpByField.value = opMap;
  }

  /** True iff the working draft differs from the last-loaded baseline. */
  const hasUnsavedChanges = computed<boolean>(() => {
    for (const field of LIST_FIELDS) {
      const a = draft.value[field];
      const b = baseline.value[field];
      if (a.length !== b.length) return true;
      for (let i = 0; i < a.length; i++) {
        if (a[i] !== b[i]) return true;
      }
    }
    return false;
  });

  async function load(): Promise<void> {
    loading.value = true;
    lastError.value = null;
    try {
      const res = await apiJson<PolicyResponse>("GET", "/api/security/policy");
      version.value = res.version ?? 0;
      projectFromRules(res.rules ?? []);
      readOnly.value = false;
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        projectFromRules([]);
      } else {
        lastError.value = e instanceof Error ? e.message : String(e);
      }
    } finally {
      loading.value = false;
    }
  }

  function addEntry(field: ListField): void {
    draft.value[field] = [...draft.value[field], ""];
  }

  function updateEntry(field: ListField, idx: number, value: string): void {
    const next = [...draft.value[field]];
    next[idx] = value;
    draft.value[field] = next;
  }

  function removeEntry(field: ListField, idx: number): void {
    draft.value[field] = draft.value[field].filter((_, i) => i !== idx);
  }

  function reset(): void {
    draft.value = structuredClone(baseline.value);
  }

  /** Fold the four categories (+ preserved others) back into a rule array.
   *
   * The ``op`` written back for each pattern preserves the pattern's
   * ORIGINAL ``op`` when it was loaded — see ``originalOpByField``. A
   * newly-added pattern (no entry in that map) uses ``FIELD_RULE[field].op``
   * (the canonical op for the category). This keeps ``op=any`` rules
   * surfaced in ``write_allow`` / ``write_deny`` from silently narrowing
   * to ``op=write`` on save.
   */
  function foldToRules(): PolicyRuleDTO[] {
    const rules: PolicyRuleDTO[] = [...otherRules.value];
    for (const field of LIST_FIELDS) {
      const spec = FIELD_RULE[field];
      const originals = originalOpByField.value[field];
      let n = 0;
      for (const raw of draft.value[field]) {
        const pattern = raw.trim();
        if (!pattern) continue; // skip blank rows (V1 ignores empties)
        const originalOp = originals[pattern];
        rules.push({
          rule_id: `${field}-${n++}-${crypto.randomUUID()}`,
          scope: "path",
          pattern,
          case_sensitive: false,
          action: spec.action,
          description: "",
          op: originalOp ?? spec.op,
        });
      }
    }
    return rules;
  }

  async function save(): Promise<{ ok: boolean; needsReboot: boolean }> {
    saving.value = true;
    saveStatus.value = "";
    lastError.value = null;
    try {
      const res = await apiJson<PolicyResponse>("PUT", "/api/security/policy", {
        rules: foldToRules(),
        reboot_reason: "policy changed",
      });
      version.value = res.version ?? version.value;
      projectFromRules(res.rules ?? []);
      saveStatus.value = "success";
      return { ok: true, needsReboot: Boolean(res.needs_reboot) };
    } catch (e) {
      if (e instanceof ApiError && (e.status === 403 || e.status === 405)) {
        readOnly.value = true;
        lastError.value = "read-only";
      } else {
        saveStatus.value = "error";
        lastError.value = e instanceof Error ? e.message : String(e);
      }
      return { ok: false, needsReboot: false };
    } finally {
      saving.value = false;
      if (saveStatus.value) {
        setTimeout(() => {
          saveStatus.value = "";
        }, 3000);
      }
    }
  }

  return {
    // state
    loading,
    saving,
    readOnly,
    version,
    saveStatus,
    lastError,
    draft,
    hasUnsavedChanges,
    // actions
    load,
    addEntry,
    updateEntry,
    removeEntry,
    reset,
    save,
  };
}
