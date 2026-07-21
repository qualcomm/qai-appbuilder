// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Skills store — shared skill registry + enabled-count (V1 parity).
 *
 * V1 keeps a single `useSkills` composable instance whose
 * `enabledSkillsCount` (`useSkills.js:22-27`, skills with mode !== 'off')
 * drives BOTH the composer's `⚡ N skills active` indicator
 * (`index.html:1185-1189`) and the sidebar "Skills" nav badge
 * (`app.js:2000-2001`). The V2 `useSkills` composable is a factory that
 * creates an independent `skills` ref per call, so the composer and the
 * sidebar could never share the count. This pinia store is that single
 * source of truth.
 *
 * Endpoints (all real, V1-shaped — directory scan + forge.config persist):
 *   GET  /api/skills                  → { skills: [...] }
 *   POST /api/skills/{id}/set_mode    → { skill_id, mode, npu_optimized }
 *   POST /api/skills/reload           → { status: "reloaded" }
 */
import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { useI18n } from "vue-i18n";

import { apiJson } from "@/api";
import { useToastStore } from "@/stores/toast";

export type SkillMode = "off" | "cloud" | "local" | "both";

export interface Skill {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  icon?: string;
  tags?: string[];
  use_for?: string;
  skill_path?: string;
  npu_optimized?: boolean;
  mode?: SkillMode;
}

interface RawSkill extends Omit<Skill, "id"> {
  id?: string;
  skill_id?: string;
}

interface SkillListResponse {
  skills: RawSkill[];
}

export const useSkillsStore = defineStore("skills", () => {
  const skills = ref<Skill[]>([]);
  const loading = ref(false);
  const loaded = ref(false);

  const toast = useToastStore();
  const { t } = useI18n();

  /**
   * Count of skills active for any model type — mode !== 'off'
   * (V1 `useSkills.js:22-27`). Drives the composer indicator + nav badge.
   */
  const enabledSkillsCount = computed(
    () =>
      skills.value.filter((s) => {
        const m = s.mode ?? (s.enabled ? "cloud" : "off");
        return m !== "off";
      }).length,
  );

  /**
   * The subset of skills globally enabled (mode !== 'off') — i.e. the pool a
   * multi-Agent role may pick from in AgentRoleForm's SKILL multi-select. A
   * role's `config.enabled_skills` whitelist may only reference these ids
   * (Settings gates the global on/off; the role narrows within that).
   */
  const enabledSkills = computed(() =>
    skills.value.filter((s) => {
      const m = s.mode ?? (s.enabled ? "cloud" : "off");
      return m !== "off";
    }),
  );

  async function fetchSkills(): Promise<void> {
    loading.value = true;
    try {
      const res = await apiJson<SkillListResponse>("GET", "/api/skills");
      skills.value = (res.skills || []).map((s) => {
        const mode: SkillMode = s.mode ?? (s.enabled ? "cloud" : "off");
        return {
          ...s,
          id: s.skill_id ?? s.id ?? "",
          mode,
          enabled: mode !== "off",
        };
      });
      loaded.value = true;
    } catch {
      skills.value = [];
    } finally {
      loading.value = false;
    }
  }

  /** Fetch once per app session (idempotent), for badge/indicator consumers. */
  async function ensureLoaded(): Promise<void> {
    if (loaded.value || loading.value) return;
    await fetchSkills();
  }

  async function reloadSkills(): Promise<void> {
    loading.value = true;
    try {
      await apiJson<{ status: string }>("POST", "/api/skills/reload");
      toast.push({
        id: crypto.randomUUID(),
        kind: "success",
        message: t("skills.reloaded"),
        timeoutMs: 5000,
      });
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: t("skills.reloadFailed") + (e as Error).message,
        timeoutMs: 5000,
      });
    } finally {
      loading.value = false;
    }
    await fetchSkills();
  }

  async function setSkillMode(id: string, mode: SkillMode): Promise<void> {
    const prev = skills.value.find((s) => s.id === id);
    const prevMode = prev?.mode;
    const name = prev?.name ?? id;
    skills.value = skills.value.map((s) =>
      s.id === id ? { ...s, mode, enabled: mode !== "off" } : s,
    );
    try {
      await apiJson<{ skill_id: string; mode: string; npu_optimized: boolean }>(
        "POST",
        `/api/skills/${encodeURIComponent(id)}/set_mode`,
        { mode },
      );
      // V1 parity: success toast `"name" → <mode label>`
      const modeLabels: Record<SkillMode, string> = {
        off: t("skills.modeResultOff"),
        cloud: t("skills.modeResultCloud"),
        local: t("skills.modeResultLocal"),
        both: t("skills.modeResultBoth"),
      };
      toast.push({
        id: crypto.randomUUID(),
        kind: "success",
        message: `"${name}" → ${modeLabels[mode]}`,
        timeoutMs: 5000,
      });
    } catch (e) {
      // rollback
      skills.value = skills.value.map((s) =>
        s.id === id
          ? { ...s, mode: prevMode, enabled: (prevMode ?? "off") !== "off" }
          : s,
      );
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: t("skills.setModeFailed") + (e as Error).message,
        timeoutMs: 5000,
      });
    }
  }

  async function toggleSkill(id: string, enabled: boolean): Promise<void> {
    await setSkillMode(id, enabled ? "cloud" : "off");
  }

  return {
    skills,
    loading,
    loaded,
    enabledSkillsCount,
    enabledSkills,
    fetchSkills,
    ensureLoaded,
    reloadSkills,
    setSkillMode,
    toggleSkill,
  };
});
