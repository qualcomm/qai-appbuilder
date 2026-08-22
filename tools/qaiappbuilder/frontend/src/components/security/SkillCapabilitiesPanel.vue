<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * SkillCapabilitiesPanel — Security tab > Skill panel.
 *
 * Two sections, both editable:
 *   1. built-in features (`source === "features"`)
 *   2. agent skills (`source === "skills"`)
 *
 * Each card carries the read-only effective path summary, the inline
 * `read` / `write` / `trusted_binaries` editor, and the per-skill 4-state
 * run mode. Agent skills are NOT a separate read-only class: a skill that
 * ships no `skill.policy.json` gets its file surface precisely by having
 * an operator policy authored here (the `skill_allow_provider` synthesises
 * a capability from the override), so it renders as a normal card.
 *
 * The exec-profile table that used to sit at the bottom of this panel
 * moved to Tool Safety layer 3 (`ExecProfilesTable`): profiles gate
 * *commands*, not skills, and their master switch
 * (`command_policy_enabled`) lives there.
 *
 * Endpoints consumed:
 *   - GET  /api/security/skill-discovery       → discovery (+ mode)
 *   - PUT  /api/security/skill_policy/{name}   → save per-skill override
 *   - GET  /api/skills/policy                  → global policy tier
 *   - POST /api/skills/{name}/set_mode         → per-skill run mode
 *
 * Composable owns the state machine (``useSkillCapabilities``); the
 * card visual (``SkillCard.vue``) handles per-skill rendering. This
 * panel stays a thin shell coordinating sections (AGENTS.md need A:
 * cohesion / .vue ≤600 lines).
 *
 * Uses global CSS classes from security.css (.sec-cfg-skill-*).
 */
import { onMounted } from "vue";
import { useI18n } from "vue-i18n";

import SkillCard from "@/components/security/SkillCard.vue";
import { useSkillCapabilities } from "@/composables/useSkillCapabilities";

const { t } = useI18n();

const {
  loading,
  savingSkill,
  filterText,
  globalMode,
  editingSkill,
  skillDraft,
  filteredFeatureSkillEntries,
  filteredAgentSkillEntries,
  skillCount,
  activeSkillCount,
  policySkillCount,
  fetchDiscoveredSkills,
  refreshAll,
  startEdit,
  cancelEdit,
  addDraftEntry,
  removeDraftEntry,
  updateDraftEntry,
  saveSkillPolicy,
  revokeSkillPolicy,
  setMode,
} = useSkillCapabilities();

onMounted(() => {
  void refreshAll();
});
</script>

<template>
  <div class="security-section">
    <!-- ── Header: title + counts + filter + refresh ─────────────────── -->
    <div class="sec-cfg-block-header">
      <span class="sec-cfg-block-title">
        {{ t("security.skillPoliciesTitle", { n: skillCount }) }}
        <span
          v-if="activeSkillCount > 0"
          class="sec-cfg-skill-active-count"
        >
          {{ t("security.skillActiveCount", { n: activeSkillCount }) }}
        </span>
        <span
          v-if="policySkillCount > 0"
          class="sec-cfg-skill-active-count"
        >
          {{ t("security.skillPolicyCount", { n: policySkillCount }) }}
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

    <!-- ── Section 1: Built-in features ──────────────────────────────── -->
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
        @revoke="revokeSkillPolicy"
        @add-entry="addDraftEntry"
        @remove-entry="removeDraftEntry"
        @update-entry="updateDraftEntry"
        @set-mode="setMode"
      />
    </div>

    <!-- ── Section 2: Agent skills ───────────────────────────────────── -->
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
    <div
      v-else-if="filteredAgentSkillEntries.length"
      class="sec-cfg-skill-grid"
    >
      <SkillCard
        v-for="[name, meta] in filteredAgentSkillEntries"
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
        @revoke="revokeSkillPolicy"
        @add-entry="addDraftEntry"
        @remove-entry="removeDraftEntry"
        @update-entry="updateDraftEntry"
        @set-mode="setMode"
      />
    </div>
  </div>
</template>
