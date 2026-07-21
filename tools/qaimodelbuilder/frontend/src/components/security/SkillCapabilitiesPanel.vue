<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * SkillCapabilitiesPanel — Security tab > Skill panel (V1 parity).
 *
 * Replaces the V2 mode-only switcher with the V1 "Skill Capabilities"
 * surface: built-in features section + agent skills section + inline
 * read/write/trusted_binaries editor + Exec Profiles read-only table.
 * The V2 mode switch (`off` / `local` / `cloud` / `both`) is preserved
 * as an enhancement at the bottom of each card (AGENTS.md need B:
 * functional alignment + per-task enhancement, never regression).
 *
 * Endpoints consumed:
 *   - GET  /api/security/skill-discovery       → discovery (V1-aligned)
 *   - PUT  /api/security/skill_policy/{name}   → save per-skill override
 *   - GET  /api/security/exec_profiles         → exec broker profiles
 *   - GET  /api/skills/policy                  → V2 global mode (enhancement)
 *   - POST /api/skills/set_mode                → V2 per-skill mode (enhancement)
 *
 * Composable owns the state machine (``useSkillCapabilities``); the
 * card visual (``SkillCard.vue``) handles per-skill rendering. This
 * panel stays a thin shell coordinating sections (AGENTS.md need A:
 * cohesion / .vue ≤600 lines).
 *
 * Uses global CSS classes from security.css (.sec-cfg-skill-*).
 */
import { computed, onMounted } from "vue";
import { useI18n } from "vue-i18n";

import SkillCard from "@/components/security/SkillCard.vue";
import { useSkillCapabilities } from "@/composables/useSkillCapabilities";

const { t } = useI18n();

const {
  loading,
  loadingExecProfiles,
  savingSkill,
  filterText,
  execProfiles,
  globalMode,
  editingSkill,
  skillDraft,
  filteredFeatureSkillEntries,
  filteredAgentSkillEntries,
  skillCount,
  activeSkillCount,
  fetchDiscoveredSkills,
  fetchExecProfiles,
  refreshAll,
  startEdit,
  cancelEdit,
  addDraftEntry,
  removeDraftEntry,
  updateDraftEntry,
  saveSkillPolicy,
  setMode,
} = useSkillCapabilities();

// Split agent skills into "with policy" (full card) vs "without policy"
// (compact row). V1 ``SecurityConfigPanel.js:1660-1756`` treats them
// as visually distinct sub-sections; the composable returns them in a
// single list so we partition here.
const agentSkillsWithPolicy = computed(() =>
  filteredAgentSkillEntries.value.filter(([, m]) => m.has_policy),
);
const agentSkillsWithoutPolicy = computed(() =>
  filteredAgentSkillEntries.value.filter(([, m]) => !m.has_policy),
);

onMounted(() => {
  void refreshAll();
});
</script>

<template>
  <div class="security-section">
    <!-- ── Header: title + active count + filter + refresh ──────────── -->
    <div class="sec-cfg-block-header">
      <span class="sec-cfg-block-title">
        {{ t("security.skillPoliciesTitle", { n: skillCount }) }}
        <span
          v-if="activeSkillCount > 0"
          class="sec-cfg-skill-active-count"
        >
          {{ t("security.skillActiveCount", { n: activeSkillCount }) }}
        </span>
      </span>
      <div class="sec-cfg-audit-controls">
        <input
          v-model="filterText"
          type="text"
          class="sec-cfg-audit-pathfilter"
          :placeholder="t('security.filterSkillsPlaceholder')"
          style="min-width: 180px;"
        />
        <span class="config-comment">
          {{ t("security.globalModeLabel") }} <strong>{{ globalMode }}</strong>
        </span>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="loading"
          @click="fetchDiscoveredSkills"
        >
          ↺ {{ t("common.refresh") }}
        </button>
      </div>
    </div>

    <div
      class="sec-cfg-list-desc"
      style="margin-bottom: var(--space-4);"
    >
      {{ t("security.skills.desc") }}
    </div>

    <!-- ── Section 1: Built-in features (V1 source === "features") ──── -->
    <div class="sec-cfg-skill-section-header">
      {{ t("security.skills.builtinSection") }}
      <span class="sec-cfg-skill-section-sub">
        {{ t("security.skills.builtinSub") }}
      </span>
    </div>
    <div
      v-if="!filteredFeatureSkillEntries.length && !loading"
      class="sec-cfg-empty"
      style="margin-bottom: var(--space-3);"
    >
      {{ t("security.skills.noPolicies.feature") }}
    </div>
    <div
      v-else-if="filteredFeatureSkillEntries.length"
      class="sec-cfg-skill-grid"
    >
      <SkillCard
        v-for="[name, meta] in filteredFeatureSkillEntries"
        :key="name"
        :skill-name="name"
        :meta="meta"
        variant="feature"
        :editing="editingSkill === name"
        :saving="savingSkill"
        :draft="skillDraft"
        @start-edit="startEdit"
        @cancel="cancelEdit"
        @save="saveSkillPolicy"
        @add-entry="addDraftEntry"
        @remove-entry="removeDraftEntry"
        @update-entry="updateDraftEntry"
        @set-mode="setMode"
      />
    </div>

    <!-- ── Section 2: Agent skills (V1 source === "skills") ──────────── -->
    <div
      class="sec-cfg-skill-section-header"
      style="margin-top: var(--space-5);"
    >
      {{ t("security.skills.agentSection") }}
      <span class="sec-cfg-skill-section-sub">
        {{ t("security.skills.agentSub") }}
      </span>
    </div>
    <div
      v-if="!filteredAgentSkillEntries.length && !loading"
      class="sec-cfg-empty"
    >
      {{ t("security.skills.noPolicies.agent") }}
    </div>

    <!-- 2a: Agent skills with policy = full card -->
    <div
      v-if="agentSkillsWithPolicy.length"
      class="sec-cfg-skill-grid"
    >
      <SkillCard
        v-for="[name, meta] in agentSkillsWithPolicy"
        :key="name"
        :skill-name="name"
        :meta="meta"
        variant="agent"
        :editing="editingSkill === name"
        :saving="savingSkill"
        :draft="skillDraft"
        @start-edit="startEdit"
        @cancel="cancelEdit"
        @save="saveSkillPolicy"
        @add-entry="addDraftEntry"
        @remove-entry="removeDraftEntry"
        @update-entry="updateDraftEntry"
        @set-mode="setMode"
      />
    </div>

    <!-- 2b: Agent skills without policy = compact row + inline editor -->
    <div
      v-if="agentSkillsWithoutPolicy.length"
      class="sec-cfg-skill-no-policy-list"
    >
      <SkillCard
        v-for="[name, meta] in agentSkillsWithoutPolicy"
        :key="name"
        :skill-name="name"
        :meta="meta"
        variant="agent-empty-row"
        :editing="editingSkill === name"
        :saving="savingSkill"
        :draft="skillDraft"
        @start-edit="startEdit"
        @cancel="cancelEdit"
        @save="saveSkillPolicy"
        @add-entry="addDraftEntry"
        @remove-entry="removeDraftEntry"
        @update-entry="updateDraftEntry"
        @set-mode="setMode"
      />
    </div>

    <!-- ── Section 3: Exec Profiles (read-only V1 table) ────────────── -->
    <div style="margin-top: var(--space-5);">
      <div class="sec-cfg-block-header">
        <div class="sec-cfg-block-title">
          🔒 {{ t("execBroker.profiles") }}
        </div>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="loadingExecProfiles"
          @click="fetchExecProfiles"
        >
          ↺ {{ t("common.refresh") }}
        </button>
      </div>
      <div
        class="sec-cfg-list-desc"
        style="margin-bottom: var(--space-3);"
      >
        {{ t("execBroker.profilesDesc") }}
      </div>

      <div
        v-if="loadingExecProfiles && !execProfiles.length"
        class="sec-cfg-empty"
      >
        Loading exec profiles…
      </div>
      <div
        v-else-if="!execProfiles.length"
        class="sec-cfg-empty"
        style="font-size: var(--text-xs);"
      >
        {{ t("execBroker.noProfiles") }}
      </div>
      <div
        v-else
        class="sec-cfg-audit-tablewrap"
      >
        <table class="sec-cfg-audit-table">
          <thead>
            <tr>
              <th style="width: 180px;">
                {{ t("execBroker.colName") }}
              </th>
              <th style="width: 220px;">
                {{ t("execBroker.colAllowedCommands") }}
              </th>
              <th>{{ t("execBroker.colDeniedPatterns") }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(prof, i) in execProfiles"
              :key="`ep-${i}`"
            >
              <td class="mono">
                {{ prof.name || "-" }}
              </td>
              <td class="mono">
                <span v-if="prof.allowed_commands.length">
                  {{ prof.allowed_commands.join(", ") }}
                </span>
                <span
                  v-else
                  style="color: var(--text-muted);"
                >—</span>
              </td>
              <td class="mono sec-cfg-audit-reason">
                <span v-if="prof.deny_patterns.length">
                  {{ prof.deny_patterns.join(", ") }}
                </span>
                <span
                  v-else
                  style="color: var(--text-muted);"
                >—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
