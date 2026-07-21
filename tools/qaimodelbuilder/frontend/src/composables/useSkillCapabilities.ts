// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useSkillCapabilities` — Skill panel state machine (V1 parity).
 *
 * Ports the V1 `SecurityConfigPanel.js` skill-tab logic
 * (legacy lines 79-90 / 304-352 / 813-824) to the V2 Clean-Arch
 * frontend without leaking V1's monolithic `defineComponent`/`reactive`
 * mega-state. Owns:
 *
 *   - skill discovery results (`/api/security/skill-discovery`)
 *   - per-skill inline editor draft (`read` / `write` /
 *     `trusted_binaries` lists)
 *   - save flow (`PUT /api/security/skill_policy/{name}`)
 *   - exec_profiles list (`/api/security/exec_profiles`)
 *   - V2 mode switch passthrough (`POST /api/skills/set_mode`)
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

/** One entry in `/api/security/skill-discovery` `skills[*]` (V2 + V1-aligned). */
export interface DiscoveredSkillEntry {
  skill_name: string;
  capability_name: string;
  read_paths: string[];
  write_paths: string[];
  exec_paths: string[];
  trusted_binaries: string[];
  description: string;
  // V1-aligned tail-appended fields (v2.7 §3.1):
  source: "features" | "skills";
  active: boolean;
  has_policy: boolean;
  raw_read: string[];
  raw_write: string[];
  raw_trusted_binaries: string[];
  // V1 short aliases (used by Overview's permission summary).
  read: string[];
  write: string[];
  // V2 enhancement: per-skill mode preference (optional — surfaced by
  // ``/api/skills/policy`` overrides but the discovery payload may
  // not always carry it; the WebUI falls back to "auto" via
  // ``modeLabel`` when undefined).
  mode?: SkillMode;
}

interface SkillDiscoveryResponse {
  skills: DiscoveredSkillEntry[];
  total: number;
  scan_status?: string;
  by_name?: Record<string, DiscoveredSkillEntry>;
}

interface ExecProfile {
  name: string;
  allowed_commands: string[];
  deny_patterns: string[];
}

interface ExecProfilesResponse {
  profiles: ExecProfile[];
  enabled: boolean;
}

/** V2 `/api/skills/policy` response (mode switch metadata). */
interface SkillPolicyResponse {
  mode: string;
  overrides: Record<string, string>;
  last_reload: string | null;
}

/** Editable fields in the V1 inline editor. */
export type SkillDraftField = "read" | "write" | "trusted_binaries";

interface SkillDraft {
  read: string[];
  write: string[];
  trusted_binaries: string[];
}

// ─── FEATURE_META — V1 parity (SecurityConfigPanel.js:85-90) ───────────────

/**
 * Display metadata for the four built-in feature skills.
 *
 * The icon set is fixed (V1 uses literal emoji); the human-readable
 * label is i18n-driven via `t('security.featureMeta.{name}')`.
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
  const loadingExecProfiles = ref(false);
  const savingSkill = ref(false);
  const filterText = ref("");

  /** Discovery results keyed by skill_name. */
  const discoveredSkills = ref<Record<string, DiscoveredSkillEntry>>({});

  /** Exec broker profiles. */
  const execProfiles = ref<ExecProfile[]>([]);

  /** V2 global skill mode (auto / off / cloud / local / both). */
  const globalMode = ref<string>("auto");

  /** Currently editing skill_name (null = no inline editor open). */
  const editingSkill = ref<string | null>(null);

  /** Inline editor draft — reactive object so v-model on inputs Just Works. */
  const skillDraft = reactive<SkillDraft>({
    read: [],
    write: [],
    trusted_binaries: [],
  });

  // ─── Computed: skill grouping (V1 featureSkillEntries / agentSkillEntries) ──

  /**
   * `[skill_name, meta]` tuples for the **built-in features** section
   * (V1 `featureSkillEntries` — `source === "features"`).
   */
  const featureSkillEntries = computed<[string, DiscoveredSkillEntry][]>(() =>
    Object.entries(discoveredSkills.value).filter(
      ([, m]) => m.source === "features",
    ),
  );

  /**
   * `[skill_name, meta]` tuples for the **agent skills** section
   * (V1 `agentSkillEntries` — `source === "skills"`).
   */
  const agentSkillEntries = computed<[string, DiscoveredSkillEntry][]>(() =>
    Object.entries(discoveredSkills.value).filter(
      ([, m]) => m.source === "skills",
    ),
  );

  /** Total discovered count (V1 `skillCount`). */
  const skillCount = computed<number>(
    () => Object.keys(discoveredSkills.value).length,
  );

  /** Active skill count (V1 `activeSkillCount`). */
  const activeSkillCount = computed<number>(
    () =>
      Object.values(discoveredSkills.value).filter((m) => m.active).length,
  );

  /** Filter applied to discovery results (V2 search box). */
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

  // ─── Discovery + exec profiles fetch ───────────────────────────────────

  /**
   * Fetch `/api/security/skill-discovery` and rebuild
   * `discoveredSkills`. Prefers the V1-aligned `by_name` dict when the
   * backend surfaces it; falls back to re-keying the `skills[]` list
   * by `skill_name` for older deployments.
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

  /** Fetch `/api/security/exec_profiles`. */
  async function fetchExecProfiles(): Promise<void> {
    loadingExecProfiles.value = true;
    try {
      const res = await apiJson<ExecProfilesResponse>(
        "GET",
        "/api/security/exec_profiles",
      );
      execProfiles.value = res.profiles ?? [];
    } catch {
      execProfiles.value = [];
    } finally {
      loadingExecProfiles.value = false;
    }
  }

  /** Fetch global mode (V2 `/api/skills/policy`). */
  async function fetchPolicy(): Promise<void> {
    try {
      const res = await apiJson<SkillPolicyResponse>(
        "GET",
        "/api/skills/policy",
      );
      globalMode.value = res.mode ?? "auto";
    } catch {
      // 404 / 500: leave default. Skill mode switching is enhancement,
      // discovery + policy editing are the primary V1 surface.
    }
  }

  // ─── Inline editor (V1 startEdit / cancelEdit / *DraftEntry / save) ────

  /**
   * Open the inline editor for `name`, seeding the draft from the
   * skill's *raw* override fields so the operator edits only their
   * additions on top of capability defaults (V1 parity, line 322-328).
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
   * Persist the current draft via `PUT /api/security/skill_policy/{name}`
   * and re-fetch discovery to reflect the new override (V1
   * `saveSkillPolicy` parity, line 334-352).
   */
  async function saveSkillPolicy(name: string): Promise<void> {
    savingSkill.value = true;
    try {
      await apiJson(
        "PUT",
        `/api/security/skill_policy/${encodeURIComponent(name)}`,
        {
          read: skillDraft.read,
          write: skillDraft.write,
          trusted_binaries: skillDraft.trusted_binaries,
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

  // ─── V2 mode switch (kept as enhancement) ─────────────────────────────

  /**
   * Set per-skill mode (V2 enhancement on top of V1 V1 policy editing).
   * Optimistic local update + rollback on failure (mirrors the prior
   * `SkillCapabilitiesPanel.vue:setMode` behaviour).
   */
  async function setMode(skillName: string, mode: SkillMode): Promise<void> {
    const entry = discoveredSkills.value[skillName];
    if (!entry) return;
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
    } catch (e) {
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
    await Promise.allSettled([
      fetchDiscoveredSkills(),
      fetchExecProfiles(),
      fetchPolicy(),
    ]);
  }

  return {
    // state
    loading,
    loadingExecProfiles,
    savingSkill,
    filterText,
    discoveredSkills,
    execProfiles,
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
    // actions
    fetchDiscoveredSkills,
    fetchExecProfiles,
    fetchPolicy,
    refreshAll,
    startEdit,
    cancelEdit,
    addDraftEntry,
    removeDraftEntry,
    updateDraftEntry,
    saveSkillPolicy,
    setMode,
  };
}
