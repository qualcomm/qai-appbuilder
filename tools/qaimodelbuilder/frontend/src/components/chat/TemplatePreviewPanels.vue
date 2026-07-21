<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  TemplatePreviewPanels — read-only preview panels for the template library.

  Presentational only (no emits, no CRUD): given the agent / team / mode currently
  flagged for preview, it renders a compact inline panel for whichever one is set.
  Extracted from TemplateLibraryDialog to keep that orchestration component under
  the §3.6 1000-line soft cap (the previews + their label helpers + their CSS are
  self-contained and reusable). Owns its own cloud-model + mode catalogs so the
  member/system models render by friendly name, not raw id.

  Theme tokens only; visual language matches the host's .tl-* preview styles.
-->
<script setup lang="ts">
import { onMounted } from "vue";
import { useI18n } from "vue-i18n";

import { useCloudModelOptions } from "@/composables/chat/useCloudModelOptions";
import { useTemplateI18n } from "@/composables/chat/useTemplateI18n";
import { useModeTemplateStore } from "@/stores/modeTemplate";
import { detectPresetTier, type ModeToolPolicy } from "@/lib/modePolicy";
import type { AgentTemplateView } from "@/stores/agentTemplate";
import type {
  RosterTemplateView,
  RosterTemplateMemberView,
} from "@/stores/rosterTemplate";
import type { ModeTemplateView } from "@/stores/modeTemplate";

defineProps<{
  agent?: AgentTemplateView | null;
  roster?: RosterTemplateView | null;
  mode?: ModeTemplateView | null;
}>();

const { t } = useI18n();
const { resolve: resolveI18n } = useTemplateI18n();
const modes = useModeTemplateStore();
const { cloudModels, cloudModelLabel, loadCloudModels } = useCloudModelOptions();

onMounted(() => {
  void loadCloudModels();
});

/** Friendly label for any (cloud) model id; "" / missing → em-dash. */
function modelLabelById(id: string | undefined): string {
  if (id === undefined || id === "") return "—";
  const hit = cloudModels.value.find((m) => m.model_id === id);
  return hit ? cloudModelLabel(hit) : id;
}

/** Human label for a mode's bound system model ("" → em-dash). */
function systemModelLabel(id: string): string {
  return modelLabelById(id === "" ? undefined : id);
}

/** Localised label for a mode's tool-policy tier (A/B/C/custom). */
function toolPolicyLabel(m: ModeTemplateView): string {
  const tier = detectPresetTier(m.toolPolicy as ModeToolPolicy);
  return tier === "custom"
    ? t("chat.discussion.modes.toolPolicyCustom")
    : t(`chat.discussion.modes.toolPolicyTier${tier}`);
}

/** Localised label for a mode's speaker strategy. */
function speakerLabel(m: ModeTemplateView): string {
  return m.flowPolicy?.speaker_strategy === "round_robin"
    ? t("chat.discussion.modes.speakerRoundRobin")
    : t("chat.discussion.modes.speakerManager");
}

/** True when the mode's tool policy is a hand-tuned custom set (not a preset). */
function isCustomTier(m: ModeTemplateView): boolean {
  return detectPresetTier(m.toolPolicy as ModeToolPolicy) === "custom";
}

/** Per-tool allow/deny entries for the custom tool-policy detail. */
function customToolEntries(m: ModeTemplateView): Array<[string, string]> {
  const tools = (m.toolPolicy as ModeToolPolicy)?.tools ?? {};
  return Object.entries(tools).map(([k, v]) => [k, String(v)]);
}

/** Localised allow / deny label for the custom tool-policy detail. */
function policyValueLabel(v: string): string {
  return v === "deny"
    ? t("chat.discussion.modes.policyDeny")
    : t("chat.discussion.modes.policyAllow");
}

/** Friendly default-mode name for a team preview ("" → em-dash). */
function rosterDefaultModeLabel(r: RosterTemplateView): string {
  if (!r.defaultModeId) return "—";
  const hit = modes.templates.find((m) => m.id === r.defaultModeId);
  return hit ? modeName(hit) : r.defaultModeId;
}

// --- Localised built-in template text (display layer only) -----------------
// Built-in presets carry per-locale i18n maps; custom rows have none and fall
// back to their own single-language text. See useTemplateI18n.
function agentName(a: AgentTemplateView): string {
  return resolveI18n(a.nameI18n, a.name);
}
function agentDisplayName(a: AgentTemplateView): string {
  return resolveI18n(a.displayNameI18n, a.displayName);
}
function agentDescription(a: AgentTemplateView): string {
  return resolveI18n(a.descriptionI18n, a.description ?? "");
}
function agentPersona(a: AgentTemplateView): string {
  return resolveI18n(a.personaI18n, a.persona ?? "");
}
function rosterName(r: RosterTemplateView): string {
  return resolveI18n(r.nameI18n, r.name);
}
function rosterDescription(r: RosterTemplateView): string {
  return resolveI18n(r.descriptionI18n, r.description ?? "");
}
function memberName(m: RosterTemplateMemberView): string {
  return resolveI18n(m.displayNameI18n, m.displayName);
}
function memberPersona(m: RosterTemplateMemberView): string {
  return resolveI18n(m.personaI18n, m.persona ?? "");
}
function modeName(m: ModeTemplateView): string {
  return resolveI18n(m.nameI18n, m.name);
}
function modeDescription(m: ModeTemplateView): string {
  return resolveI18n(m.descriptionI18n, m.description ?? "");
}
function modeFraming(m: ModeTemplateView): string {
  return resolveI18n(m.framingI18n, m.framing ?? "");
}

</script>

<template>
  <!-- Agent preview -->
  <section
    v-if="agent != null"
    class="tl-preview"
    data-testid="library-agent-preview"
  >
    <h4 class="tl-preview-title">{{ agentName(agent) }}</h4>
    <p class="tl-preview-role">{{ agentDisplayName(agent) }}</p>
    <p v-if="agentDescription(agent)" class="tl-preview-desc">
      {{ agentDescription(agent) }}
    </p>
    <dl class="tl-preview-grid">
      <dt>{{ t("chat.discussion.modelId") }}</dt>
      <dd>{{ modelLabelById(agent.modelId) }}</dd>
      <dt>{{ t("chat.discussion.allowedTools") }}</dt>
      <dd>
        <template v-if="agent.allowedTools.length > 0">
          <span v-for="tool in agent.allowedTools" :key="tool" class="tl-chip">{{
            tool
          }}</span>
        </template>
        <span v-else>—</span>
      </dd>
      <dt>{{ t("chat.discussion.enabledSkills") }}</dt>
      <dd>
        <template v-if="agent.enabledSkills.length > 0">
          <span
            v-for="sk in agent.enabledSkills"
            :key="sk"
            class="tl-chip"
            >{{ sk }}</span
          >
        </template>
        <span v-else>—</span>
      </dd>
    </dl>
    <p v-if="agentPersona(agent)" class="tl-preview-framing">
      {{ agentPersona(agent) }}
    </p>
  </section>

  <!-- Team preview -->
  <section
    v-if="roster != null"
    class="tl-preview"
    data-testid="library-roster-preview"
  >
    <h4 class="tl-preview-title">{{ rosterName(roster) }}</h4>
    <p v-if="rosterDescription(roster)" class="tl-preview-desc">
      {{ rosterDescription(roster) }}
    </p>
    <dl class="tl-preview-grid">
      <dt>{{ t("chat.discussion.templates.defaultModeLabel") }}</dt>
      <dd>{{ rosterDefaultModeLabel(roster) }}</dd>
      <dt>{{ t("chat.discussion.templates.membersLabel") }}</dt>
      <dd>
        {{ t("chat.discussion.library.members", { n: roster.members.length }) }}
      </dd>
    </dl>
    <ul class="tl-preview-members">
      <li v-for="(m, i) in roster.members" :key="i" class="tl-preview-member">
        <details>
          <summary class="tl-preview-member-sum">
            <span class="tl-preview-member-name">{{ memberName(m) }}</span>
            <span class="tl-preview-member-model">{{
              modelLabelById(m.modelId)
            }}</span>
          </summary>
          <dl class="tl-preview-grid tl-preview-grid--nested">
            <dt>{{ t("chat.discussion.modelId") }}</dt>
            <dd>{{ modelLabelById(m.modelId) }}</dd>
            <template v-if="memberPersona(m)">
              <dt>{{ t("chat.discussion.persona") }}</dt>
              <dd class="tl-preview-framing">{{ memberPersona(m) }}</dd>
            </template>
            <dt>{{ t("chat.discussion.allowedTools") }}</dt>
            <dd>
              <template v-if="m.allowedTools.length > 0">
                <span
                  v-for="tool in m.allowedTools"
                  :key="tool"
                  class="tl-chip"
                  >{{ tool }}</span
                >
              </template>
              <span v-else>—</span>
            </dd>
            <dt>{{ t("chat.discussion.enabledSkills") }}</dt>
            <dd>
              <template v-if="m.enabledSkills.length > 0">
                <span
                  v-for="sk in m.enabledSkills"
                  :key="sk"
                  class="tl-chip"
                  >{{ sk }}</span
                >
              </template>
              <span v-else>—</span>
            </dd>
          </dl>
        </details>
      </li>
    </ul>
  </section>

  <!-- Mode preview -->
  <section
    v-if="mode != null"
    class="tl-preview"
    data-testid="library-mode-preview"
  >
    <h4 class="tl-preview-title">{{ modeName(mode) }}</h4>
    <p v-if="modeDescription(mode)" class="tl-preview-desc">
      {{ modeDescription(mode) }}
    </p>
    <dl class="tl-preview-grid">
      <dt>{{ t("chat.discussion.modes.framing") }}</dt>
      <dd class="tl-preview-framing">{{ modeFraming(mode) || "—" }}</dd>
      <dt>{{ t("chat.discussion.modes.systemModel") }}</dt>
      <dd>{{ systemModelLabel(mode.systemModel) }}</dd>
      <dt>{{ t("chat.discussion.modes.toolPolicy") }}</dt>
      <dd>
        {{ toolPolicyLabel(mode) }}
        <details
          v-if="isCustomTier(mode)"
          class="tl-tool-detail"
          data-testid="library-mode-preview-tool-detail"
        >
          <summary>{{ t("chat.discussion.modes.toolPolicyAdvanced") }}</summary>
          <ul class="tl-tool-detail-list">
            <li v-for="[tool, val] in customToolEntries(mode)" :key="tool">
              <span class="tl-tool-detail-name">{{ tool }}</span>
              <span class="tl-tool-detail-val">{{ policyValueLabel(val) }}</span>
            </li>
          </ul>
        </details>
      </dd>
      <dt>{{ t("chat.discussion.modes.speakerStrategy") }}</dt>
      <dd>{{ speakerLabel(mode) }}</dd>
      <dt>{{ t("chat.discussion.modes.flowMaxRounds") }}</dt>
      <dd>{{ mode.flowPolicy?.max_rounds ?? "—" }}</dd>
      <dt>{{ t("chat.discussion.modes.judgeEnabled") }}</dt>
      <dd>
        {{
          mode.flowPolicy?.judge_enabled !== false
            ? t("common.yes")
            : t("common.no")
        }}
      </dd>
      <dt>{{ t("chat.discussion.modes.allowModeSwitch") }}</dt>
      <dd>
        {{
          mode.flowPolicy?.allow_mode_switch !== false
            ? t("common.yes")
            : t("common.no")
        }}
      </dd>
      <template
        v-if="
          mode.hardConstraints &&
          (mode.hardConstraints.max_chars_per_turn != null ||
            mode.hardConstraints.max_seconds_per_turn != null)
        "
      >
        <dt>{{ t("chat.discussion.modes.hardConstraints") }}</dt>
        <dd>
          <span v-if="mode.hardConstraints.max_chars_per_turn != null">
            {{ t("chat.discussion.modes.maxCharsLabel") }}:
            {{ mode.hardConstraints.max_chars_per_turn }}
          </span>
          <span v-if="mode.hardConstraints.max_seconds_per_turn != null">
            {{ t("chat.discussion.modes.maxSecondsLabel") }}:
            {{ mode.hardConstraints.max_seconds_per_turn }}
          </span>
        </dd>
      </template>
    </dl>
  </section>
</template>

<style scoped>
.tl-preview {
  padding: 12px;
  border: 1px dashed var(--border);
  border-radius: 8px;
  background: var(--bg-tertiary);
  margin-bottom: 16px;
}
.tl-preview-title {
  margin: 0 0 4px;
  font-size: 0.95rem;
}
.tl-preview-desc {
  margin: 0 0 8px;
  font-size: 0.8rem;
  color: var(--text-secondary);
}
.tl-preview-grid {
  display: grid;
  grid-template-columns: minmax(0, 40%) minmax(0, 60%);
  gap: 4px 12px;
  margin: 0;
  font-size: 0.8rem;
}
.tl-preview-grid dt {
  color: var(--text-secondary);
  font-weight: 600;
}
.tl-preview-grid dd {
  margin: 0;
  color: var(--text-primary);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.tl-preview-framing {
  white-space: pre-wrap;
}
.tl-preview-role {
  margin: 0 0 8px;
  font-size: 0.82rem;
  font-weight: 500;
  color: var(--accent);
}
.tl-preview-members {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.tl-preview-member {
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-secondary);
}
.tl-preview-member-sum {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 8px;
  cursor: pointer;
  font-size: 0.8rem;
}
.tl-preview-member-name {
  font-weight: 500;
  color: var(--text-primary);
}
.tl-preview-member-model {
  font-size: 0.74rem;
  color: var(--text-secondary);
}
.tl-preview-grid--nested {
  padding: 6px 8px 8px;
}
.tl-chip {
  display: inline-block;
  margin: 0 4px 4px 0;
  padding: 1px 6px;
  font-size: 0.72rem;
  color: var(--text-secondary);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 999px;
}
.tl-tool-detail {
  margin-top: 4px;
  font-size: 0.76rem;
}
.tl-tool-detail summary {
  cursor: pointer;
  color: var(--text-secondary);
}
.tl-tool-detail-list {
  list-style: none;
  margin: 4px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.tl-tool-detail-list li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.tl-tool-detail-name {
  font-family: var(--font-mono, monospace);
  color: var(--text-primary);
}
.tl-tool-detail-val {
  color: var(--text-secondary);
}
</style>
