// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useSkillCapabilities` — Security > Skill panel state machine.
 *
 * Owns the whole per-skill capability surface:
 *
 *   - discovery results (`GET /api/security/skill-discovery`) — registry
 *     capabilities + operator overrides + filesystem-scanned skills,
 *     each carrying `active` / `has_policy` / `mode`
 *   - the inline path-policy editor draft (`read` / `write` /
 *     `trusted_binaries`) and its save flow
 *     (`PUT /api/security/skill_policy/{name}`)
 *   - the per-skill 4-state run mode (`POST /api/skills/{id}/set_mode`)
 *
 * What the editor actually does (this is load-bearing, and was briefly
 * mistaken for dead code): the saved lists land in the security runtime
 * state `skill_policies` bucket, persisted to `forge_config.json`. The
 * `skill_allow_provider` wired in `apps/api/_security_di.py` merges that
 * bucket onto each registered capability — and synthesises a capability
 * for a skill that has ONLY an override — before handing the result to
 * `CheckPermissionUseCase`'s skill-capability ALLOW short-circuit. So a
 * path added here is a path FileGuard really allows for that skill, with
 * no restart. `trusted_binaries` additionally feeds `exec_paths` so an
 * authorised binary becomes executable (the exec-deny hard gate is still
 * re-checked downstream). A skill switched to mode `off` unlocks nothing,
 * whatever its policy says.
 *
 * Exec-profile state deliberately does NOT live here: profiles gate
 * *commands*, not skills — see `useExecProfiles` + `ExecProfilesTable`,
 * rendered in Tool Safety layer 3 next to the `command_policy_enabled`
 * switch that turns their broker on.
 *
 * The host component (`SkillCapabilitiesPanel.vue`) stays a thin
 * template shell (AGENTS.md need A: cohesion / `.vue` ≤600 lines).
 */
import { computed, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";

import { apiJson } from "@/api";
import { useToastStore } from "@/stores/toast";

// ─── Wire-format types ──────────────────────────────────────────────────────

export type SkillMode = "off" | "cloud" | "local" | "both";

/** One entry in `/api/security/skill-discovery` `skills[*]`. */
export interface DiscoveredSkillEntry {
  skill_name: string;
  /** Human-readable title extracted from SKILL.md's first `# H1`, or
   *  the frontmatter `name` as a fallback. Populated by
   *  `qai.platform.skills.SkillDiscovery`; always safe to render.
   *  Optional on the wire so older backends without the field degrade
   *  gracefully (the UI falls back to `skill_name`). */
  display_name?: string;
  capability_name: string;
  /** Effective (declaration + override) read surface. */
  read_paths: string[];
  write_paths: string[];
  exec_paths: string[];
  trusted_binaries: string[];
  description: string;
  /** `"features"` = built-in capability, `"skills"` = agent skill. */
  source: "features" | "skills";
  /** True when a `skill.policy.json` was loaded into the registry. */
  active: boolean;
  /** True when the operator saved a per-skill override for this skill. */
  has_policy: boolean;
  /** The operator's override only — what the inline editor edits. */
  raw_read: string[];
  raw_write: string[];
  raw_trusted_binaries: string[];
  /** Short aliases of `read_paths` / `write_paths` (Overview summary). */
  read: string[];
  write: string[];
  /**
   * Per-skill 4-state run mode, resolved server-side from
   * `forge.config skills.overrides[<id>].mode`. `""` / undefined means the
   * backend could not resolve one (minimal container) — the UI then shows
   * no mode selected instead of guessing.
   */
  mode?: SkillMode | "";
}

interface SkillDiscoveryResponse {
  skills: DiscoveredSkillEntry[];
  total: number;
  scan_status?: string;
  by_name?: Record<string, DiscoveredSkillEntry>;
}

/** `/api/skills/policy` — global skill policy tier (auto/manual/disabled). */
interface SkillPolicyResponse {
  mode: string;
  overrides: Record<string, string>;
  last_reload: string | null;
}

/** Editable fields in the inline editor. */
export type SkillDraftField = "read" | "write" | "trusted_binaries";

interface SkillDraft {
  read: string[];
  write: string[];
  trusted_binaries: string[];
}

/**
 * Split one field of a skill's effective surface into the two layers the
 * Security panel must present differently.
 *
 * The backend merges them (`_entry_for`: `read_paths = merge(cap.read_paths,
 * raw_read)`) and reports the union, while the editor only ever edits the
 * override half. That is why a skill showing "读取: 3 条" opened an EMPTY
 * editor: the 3 came from the factory declaration, the editor showed the (empty)
 * override, and nothing on screen said the two were different things.
 *
 * `factory` = declared in the skill's own `skill.policy.json` (the author's
 * required minimum — read-only here; the backend deliberately never mutates it).
 * `custom` = the operator's own additions, which are freely editable.
 *
 * Derived by subtraction rather than a new API field: the effective list IS the
 * union, so whatever is not in the override came from the declaration.
 */
export function splitPolicyLayers(
  effective: readonly string[] | undefined,
  override: readonly string[] | undefined,
): { factory: string[]; custom: string[] } {
  const custom = [...(override ?? [])];
  const customSet = new Set(custom);
  return {
    factory: [...(effective ?? [])].filter((p) => !customSet.has(p)),
    custom,
  };
}

// ─── FEATURE_META — V1 parity (SecurityConfigPanel.js:85-90) ───────────────

/**
 * Icons for the built-in feature skills. V1 parity
 * (SecurityConfigPanel.js:85-90).
 *
 * The human-readable **label** now comes from ``meta.display_name`` (the
 * skill's own ``SKILL.md`` first ``# H1``) — see
 * ``SkillCard.vue::skillDisplayName``. The ``security.featureMeta.<name>``
 * i18n table is retained ONLY for entries whose display name is not a
 * SKILL.md H1 (currently just ``translate``, which isn't a SKILL.md skill).
 */
export const FEATURE_ICONS: Record<string, string> = {
  "model-builder": "⚙️",
  "ppt-gen": "📊",
  "code-assist": "💻",
  translate: "🌐",
};

// ─── Composable ────────────────────────────────────────────────────────────

export function useSkillCapabilities() {
  const toast = useToastStore();
  const { t } = useI18n();

  const loading = ref(false);
  const savingSkill = ref(false);
  const filterText = ref("");

  /** Discovery results keyed by skill_name. */
  const discoveredSkills = ref<Record<string, DiscoveredSkillEntry>>({});

  /** Global skill policy tier (auto / manual / disabled). */
  const globalMode = ref<string>("auto");

  /** Currently editing skill_name (null = no inline editor open). */
  const editingSkill = ref<string | null>(null);

  /** Inline editor draft — reactive so the row inputs bind directly. */
  const skillDraft = reactive<SkillDraft>({
    read: [],
    write: [],
    trusted_binaries: [],
  });

  // ─── Computed: skill grouping ────────────────────────────────────────

  /** `[skill_name, meta]` tuples for the built-in features section. */
  const featureSkillEntries = computed<[string, DiscoveredSkillEntry][]>(() =>
    Object.entries(discoveredSkills.value).filter(
      ([, m]) => m.source === "features",
    ),
  );

  /** `[skill_name, meta]` tuples for the agent skills section. */
  const agentSkillEntries = computed<[string, DiscoveredSkillEntry][]>(() =>
    Object.entries(discoveredSkills.value).filter(
      ([, m]) => m.source === "skills",
    ),
  );

  /** Total discovered count. */
  const skillCount = computed<number>(
    () => Object.keys(discoveredSkills.value).length,
  );

  /**
   * Count of skills whose declaration is loaded into the capability
   * registry (the "已激活" badge) — NOT the run-mode count.
   */
  const activeSkillCount = computed<number>(
    () => Object.values(discoveredSkills.value).filter((m) => m.active).length,
  );

  /** Count of skills carrying an operator-authored path policy. */
  const policySkillCount = computed<number>(
    () =>
      Object.values(discoveredSkills.value).filter((m) => m.has_policy).length,
  );

  /** Filter applied to discovery results (search box). */
  function _matchesFilter(meta: DiscoveredSkillEntry): boolean {
    const q = filterText.value.toLowerCase().trim();
    if (!q) return true;
    return (
      meta.skill_name.toLowerCase().includes(q) ||
      meta.capability_name.toLowerCase().includes(q) ||
      meta.description.toLowerCase().includes(q)
    );
  }

  const filteredFeatureSkillEntries = computed<
    [string, DiscoveredSkillEntry][]
  >(() => featureSkillEntries.value.filter(([, m]) => _matchesFilter(m)));

  const filteredAgentSkillEntries = computed<
    [string, DiscoveredSkillEntry][]
  >(() => agentSkillEntries.value.filter(([, m]) => _matchesFilter(m)));

  // ─── Discovery fetch ──────────────────────────────────────────────────

  /**
   * Fetch `/api/security/skill-discovery` and rebuild
   * `discoveredSkills`. Prefers the `by_name` dict when the backend
   * surfaces it; falls back to re-keying `skills[]` by `skill_name`.
   */
  async function fetchDiscoveredSkills(): Promise<void> {
    loading.value = true;
    try {
      const res = await apiJson<SkillDiscoveryResponse>(
        "GET",
        "/api/security/skill-discovery",
      );
      if (res.by_name && typeof res.by_name === "object") {
        discoveredSkills.value = { ...res.by_name };
      } else {
        const dict: Record<string, DiscoveredSkillEntry> = {};
        for (const entry of res.skills ?? []) {
          dict[entry.skill_name] = entry;
        }
        discoveredSkills.value = dict;
      }
    } catch (e) {
      discoveredSkills.value = {};
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: t("security.loadSkillPoliciesFailed", { msg: (e as Error).message }),
        timeoutMs: 4000,
      });
    } finally {
      loading.value = false;
    }
  }

  /** Fetch the global skill policy tier (`/api/skills/policy`). */
  async function fetchPolicy(): Promise<void> {
    try {
      const res = await apiJson<SkillPolicyResponse>(
        "GET",
        "/api/skills/policy",
      );
      globalMode.value = res.mode ?? "auto";
    } catch {
      // 404 / 500: keep the default. The tier is informational here.
    }
  }

  // ─── Inline editor ────────────────────────────────────────────────────

  /**
   * Open the inline editor for `name`, seeding the draft from the skill's
   * *raw* override fields: the operator edits only their own additions,
   * never the skill author's on-disk declaration (which stays visible in
   * the read-only summary and is merged on top of at enforcement time).
   */
  function startEdit(name: string, meta: DiscoveredSkillEntry): void {
    editingSkill.value = name;
    skillDraft.read = [...(meta.raw_read ?? [])];
    skillDraft.write = [...(meta.raw_write ?? [])];
    skillDraft.trusted_binaries = [...(meta.raw_trusted_binaries ?? [])];
  }

  function cancelEdit(): void {
    editingSkill.value = null;
  }

  function addDraftEntry(field: SkillDraftField): void {
    skillDraft[field].push("");
  }

  function removeDraftEntry(field: SkillDraftField, idx: number): void {
    skillDraft[field].splice(idx, 1);
  }

  function updateDraftEntry(
    field: SkillDraftField,
    idx: number,
    val: string,
  ): void {
    skillDraft[field].splice(idx, 1, val);
  }

  /**
   * Persist the draft via `PUT /api/security/skill_policy/{name}` and
   * re-fetch discovery so the card shows the merged, enforced surface.
   */
  async function saveSkillPolicy(name: string): Promise<void> {
    savingSkill.value = true;
    try {
      await apiJson(
        "PUT",
        `/api/security/skill_policy/${encodeURIComponent(name)}`,
        {
          read: skillDraft.read.map((s) => s.trim()).filter(Boolean),
          write: skillDraft.write.map((s) => s.trim()).filter(Boolean),
          trusted_binaries: skillDraft.trusted_binaries
            .map((s) => s.trim())
            .filter(Boolean),
        },
      );
      editingSkill.value = null;
      await fetchDiscoveredSkills();
      toast.push({
        id: crypto.randomUUID(),
        kind: "success",
        message: t("security.skillPolicySaved"),
        timeoutMs: 2500,
      });
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: t("security.skillPolicySaveFailed", { msg: (e as Error).message }),
        timeoutMs: 4000,
      });
    } finally {
      savingSkill.value = false;
    }
  }

  /**
   * Revoke the operator's own policy for `name`, leaving the factory
   * declaration untouched.
   *
   * `PUT` with all three lists empty is how the backend deletes an override
   * (`write_skill_policy_override` drops the bucket rather than storing an empty
   * shell — see `skill_discovery.py`). That was already possible but entirely
   * undiscoverable: an operator had to open the editor, delete every row one by
   * one, and guess that saving nothing meant "revoke". This gives the action a
   * name and a single button.
   *
   * Only the operator's additions go away; the skill keeps every path its own
   * `skill.policy.json` declares, so revoking cannot break the skill.
   */
  async function revokeSkillPolicy(name: string): Promise<void> {
    savingSkill.value = true;
    try {
      await apiJson(
        "PUT",
        `/api/security/skill_policy/${encodeURIComponent(name)}`,
        { read: [], write: [], trusted_binaries: [] },
      );
      editingSkill.value = null;
      await fetchDiscoveredSkills();
      toast.push({
        id: crypto.randomUUID(),
        kind: "success",
        message: t("security.skillPolicyRevoked"),
        timeoutMs: 2500,
      });
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: t("security.skillPolicyRevokeFailed", {
          msg: (e as Error).message,
        }),
        timeoutMs: 4000,
      });
    } finally {
      savingSkill.value = false;
    }
  }

  // ─── Per-skill run mode ───────────────────────────────────────────────

  /**
   * Set a skill's 4-state run mode.
   *
   * Optimistic local update, then a discovery re-fetch so the card
   * reflects the SERVER's resolved mode (State-Truth-First: `local` /
   * `both` are rejected for a non-NPU skill, so the optimistic value is
   * not necessarily what got persisted). On failure the previous mode is
   * restored and the error surfaced.
   */
  async function setMode(skillName: string, mode: SkillMode): Promise<void> {
    const entry = discoveredSkills.value[skillName];
    if (!entry) return;
    const previous = entry.mode;
    entry.mode = mode;
    try {
      // Per-skill mode uses POST /api/skills/{skill_id}/set_mode with a
      // PerSkillModeRequest body {mode: off|cloud|local|both}. (The global
      // POST /api/skills/set_mode only accepts auto|manual|disabled and
      // ignores skill_id — posting there returned 422 for these modes.)
      await apiJson(
        "POST",
        `/api/skills/${encodeURIComponent(skillName)}/set_mode`,
        { mode },
      );
      await fetchDiscoveredSkills();
    } catch (e) {
      const current = discoveredSkills.value[skillName];
      if (current) current.mode = previous;
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: t("security.skillSetModeFailed", { msg: (e as Error).message }),
        timeoutMs: 4000,
      });
    }
  }

  // ─── Initial load helper ──────────────────────────────────────────────

  async function refreshAll(): Promise<void> {
    await Promise.allSettled([fetchDiscoveredSkills(), fetchPolicy()]);
  }

  return {
    // state
    loading,
    savingSkill,
    filterText,
    discoveredSkills,
    globalMode,
    editingSkill,
    skillDraft,
    // computed
    featureSkillEntries,
    agentSkillEntries,
    filteredFeatureSkillEntries,
    filteredAgentSkillEntries,
    skillCount,
    activeSkillCount,
    policySkillCount,
    // actions
    fetchDiscoveredSkills,
    fetchPolicy,
    refreshAll,
    startEdit,
    cancelEdit,
    addDraftEntry,
    removeDraftEntry,
    updateDraftEntry,
    saveSkillPolicy,
    revokeSkillPolicy,
    setMode,
  };
}
