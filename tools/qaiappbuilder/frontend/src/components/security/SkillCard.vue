<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * SkillCard — one skill in the Security > Skill panel.
 *
 * Two display states:
 *   - **Summary** (default): icon + label + skill_name + badges
 *     (declaration loaded / operator policy) + the effective read /
 *     write / trusted counts + an Edit (or "add policy") button + the
 *     4-state run-mode switch.
 *   - **Inline editor**: three list fields (`read` / `write` /
 *     `trusted_binaries`) with add / remove / edit + Save / Cancel.
 *
 * The editor is NOT decorative. Saved lists persist into the security
 * runtime state `skill_policies` bucket and are merged onto the skill's
 * on-disk declaration by the `skill_allow_provider`, which feeds
 * `CheckPermissionUseCase`'s skill-capability ALLOW short-circuit — so a
 * path added here is a path FileGuard allows for that skill, and a
 * trusted binary becomes executable (exec-deny still vetoes protected
 * paths). A skill in mode `off` unlocks nothing regardless of its policy,
 * which is why the mode switch sits on the same card.
 *
 * Every discovered skill is editable — including an agent skill that
 * ships no `skill.policy.json` (that is exactly how such a skill gets a
 * file surface: the provider synthesises a capability from the override
 * alone). Two variants only differ in chrome:
 *   - "feature": built-in capability (custom icon + i18n label).
 *   - "agent":   user-installed agent skill (⚡ icon + skill_name).
 *
 * The parent (`SkillCapabilitiesPanel.vue`) owns the editing state and
 * the draft via `useSkillCapabilities`; this child stays presentational.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";

import {
  FEATURE_ICONS,
  splitPolicyLayers,
  type DiscoveredSkillEntry,
  type SkillDraftField,
  type SkillMode,
} from "@/composables/useSkillCapabilities";

const props = defineProps<{
  skillName: string;
  meta: DiscoveredSkillEntry;
  variant: "feature" | "agent";
  editing: boolean;
  saving: boolean;
  draft: { read: string[]; write: string[]; trusted_binaries: string[] };
}>();

const emit = defineEmits<{
  (e: "start-edit", name: string, meta: DiscoveredSkillEntry): void;
  (e: "cancel"): void;
  (e: "save", name: string): void;
  (e: "revoke", name: string): void;
  (e: "add-entry", field: SkillDraftField): void;
  (e: "remove-entry", field: SkillDraftField, idx: number): void;
  (e: "update-entry", field: SkillDraftField, idx: number, val: string): void;
  (e: "set-mode", name: string, mode: SkillMode): void;
}>();

const { t, te } = useI18n();

// FEATURE_META parity (icon only; display name now comes from the skill
// itself via SKILL.md's first `# H1`, propagated as ``meta.display_name``).
//
// Precedence for the human-readable title:
//   1. ``meta.display_name`` — the H1 the skill authored;
//   2. legacy ``security.featureMeta.<name>`` i18n key (only kept for
//      features whose display name is not a SKILL.md H1, e.g. ``translate``);
//   3. ``skill_name`` — the raw id as a final failsafe.
const FEATURE_NAMES = new Set([
  "model-builder",
  "ppt-gen",
  "code-assist",
  "translate",
]);
function skillDisplayName(name: string, meta: DiscoveredSkillEntry): string {
  if (meta.display_name && meta.display_name.trim() !== "") {
    return meta.display_name;
  }
  if (FEATURE_NAMES.has(name) && te(`security.featureMeta.${name}`)) {
    return t(`security.featureMeta.${name}`);
  }
  return name;
}
function featureIcon(name: string): string {
  return FEATURE_ICONS[name] ?? "⚙️";
}

// Inline editor field metadata.
const FIELD_LABEL_KEYS: Record<SkillDraftField, string> = {
  read: "security.skills.readLabel",
  write: "security.skills.writeLabel",
  trusted_binaries: "security.skills.trustedLabel",
};
const FIELD_KEY_NAMES: Record<SkillDraftField, string> = {
  read: "required_read",
  write: "required_write",
  trusted_binaries: "trusted_binaries",
};
const EDIT_FIELDS: readonly SkillDraftField[] = [
  "read",
  "write",
  "trusted_binaries",
] as const;
function fieldPlaceholder(field: SkillDraftField): string {
  return field === "trusted_binaries"
    ? "e.g. C:/Tools/**/*.exe"
    : "e.g. ${PROJECT_ROOT}/mydir";
}

/**
 * The FACTORY (author-declared) half of each editable field.
 *
 * Rendered read-only above the operator's own rows so the editor stops looking
 * empty for a skill that plainly shows "读取: 3 条" in its summary — those 3 come
 * from `skill.policy.json` and are the skill's required minimum, which the
 * backend never mutates. Without this the two layers were indistinguishable and
 * the panel appeared to be "add-only, cannot view or delete".
 */
const factoryLayers = computed(() => ({
  read: splitPolicyLayers(props.meta.read_paths, props.meta.raw_read).factory,
  write: splitPolicyLayers(props.meta.write_paths, props.meta.raw_write).factory,
  trusted_binaries: splitPolicyLayers(
    props.meta.trusted_binaries,
    props.meta.raw_trusted_binaries,
  ).factory,
}));

/** Count of the operator's own entries (drives the revoke button's visibility). */
const customCount = computed(
  () =>
    (props.meta.raw_read?.length ?? 0) +
    (props.meta.raw_write?.length ?? 0) +
    (props.meta.raw_trusted_binaries?.length ?? 0),
);

// Per-skill run mode. ``mode`` comes from the discovery payload (resolved
// server-side from forge.config); an empty value means "unresolved" and
// leaves every button unselected rather than faking a default.
const MODES: readonly SkillMode[] = ["off", "local", "cloud", "both"] as const;
function modeLabel(mode: SkillMode): string {
  switch (mode) {
    case "off":
      return t("security.skills.modeOff");
    case "cloud":
      return t("security.skills.modeCloud");
    case "local":
      return t("security.skills.modeLocal");
    case "both":
      return t("security.skills.modeBoth");
  }
}

function onUpdate(field: SkillDraftField, idx: number, ev: Event): void {
  const target = ev.target as HTMLInputElement | null;
  if (target !== null) {
    emit("update-entry", field, idx, target.value);
  }
}
</script>

<template>
  <div
    :class="[
      'sec-cfg-skill-card',
      editing && 'sec-cfg-skill-card--editing',
    ]"
  >
    <div class="sec-cfg-skill-feature-meta">
      <template v-if="variant === 'feature'">
        <span class="sec-cfg-skill-ficon">{{ featureIcon(skillName) }}</span>
        <span class="sec-cfg-skill-flabel">{{ skillDisplayName(skillName, meta) }}</span>
        <span class="sec-cfg-skill-fid mono">{{ skillName }}</span>
      </template>
      <template v-else>
        <span class="sec-cfg-skill-ficon">⚡</span>
        <span class="sec-cfg-skill-flabel">{{ skillDisplayName(skillName, meta) }}</span>
        <span class="sec-cfg-skill-fid mono">{{ skillName }}</span>
      </template>

      <!-- "已激活" = the skill's own skill.policy.json declaration is
           loaded into the capability registry and enforced. -->
      <span
        v-if="meta.active"
        class="sec-cfg-skill-badge sec-cfg-skill-badge--active"
        :title="t('security.skills.activeHint')"
      >
        {{ t("security.skills.activeLabel") }}
      </span>
      <!-- Operator-authored policy on top of (or instead of) it. -->
      <span
        v-if="meta.has_policy"
        class="sec-cfg-skill-badge sec-cfg-skill-badge--policy"
        :title="t('security.skills.policyHint')"
      >
        {{ t("security.skills.policyLabel") }}
      </span>

      <div class="sec-cfg-skill-card-actions">
        <template v-if="!editing">
          <!-- Revoke is offered only when the operator actually has entries to
               revoke; it never touches the factory declaration. -->
          <button
            v-if="customCount > 0"
            type="button"
            class="btn btn-ghost btn-sm sec-cfg-skill-revoke"
            :disabled="saving"
            :title="t('security.skills.revokeHint')"
            @click="emit('revoke', skillName)"
          >
            {{ t("security.skills.revokeBtn") }}
          </button>
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            @click="emit('start-edit', skillName, meta)"
          >
            {{
              customCount > 0
                ? t("security.skills.editBtn")
                : t("security.skills.addPolicyBtn")
            }}
          </button>
        </template>
        <template v-else>
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            :disabled="saving"
            @click="emit('cancel')"
          >
            {{ t("security.skills.cancelBtn") }}
          </button>
          <button
            type="button"
            class="btn btn-primary btn-sm"
            :disabled="saving"
            @click="emit('save', skillName)"
          >
            {{
              customCount > 0
                ? t("security.skills.saveBtn")
                : t("security.skills.createBtn")
            }}
          </button>
        </template>
      </div>
    </div>

    <!-- Read-only summary. Split into the two layers so a count can never be
         mistaken for "things I can edit": the factory figure is the skill's own
         required minimum, the custom figure is the operator's additions. -->
    <div
      v-if="!editing"
      class="sec-cfg-skill-summary"
    >
      <span v-if="meta.read_paths.length">
        {{ t("security.skills.readSummary", { n: meta.read_paths.length }) }}
      </span>
      <span v-if="meta.write_paths.length">
        {{ t("security.skills.writeSummary", { n: meta.write_paths.length }) }}
      </span>
      <span v-if="meta.trusted_binaries.length">
        {{
          t("security.skills.trustedSummary", {
            n: meta.trusted_binaries.length,
          })
        }}
      </span>
      <span
        v-if="customCount > 0"
        class="sec-cfg-skill-summary-custom"
        :title="t('security.skills.customHint')"
      >
        {{ t("security.skills.customSummary", { n: customCount }) }}
      </span>
      <span
        v-if="
          !meta.read_paths.length &&
            !meta.write_paths.length &&
            !meta.trusted_binaries.length
        "
        class="sec-cfg-skill-summary-empty"
      >
        {{ t("security.skills.emptyPolicy") }}
      </span>
    </div>

    <!-- Inline editor -->
    <template v-else>
      <div
        v-for="field in EDIT_FIELDS"
        :key="field"
        class="sec-cfg-skill-edit-section"
      >
        <div
          class="sec-cfg-list-header"
          style="margin-top: var(--space-2);"
        >
          <div
            class="sec-cfg-list-title"
            style="font-size: var(--text-sm);"
          >
            {{ t(FIELD_LABEL_KEYS[field]) }}
            <span class="sec-cfg-list-key mono">
              {{ FIELD_KEY_NAMES[field] }}
            </span>
          </div>
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            @click="emit('add-entry', field)"
          >
            {{ t("security.skills.addEntryBtn") }}
          </button>
        </div>
        <!-- Factory declaration: visible but not editable. Shown FIRST so the
             operator sees what the skill already requires before adding to it. -->
        <div
          v-if="factoryLayers[field].length"
          class="sec-cfg-list sec-cfg-list--factory"
        >
          <div
            v-for="(entry, idx) in factoryLayers[field]"
            :key="`factory-${field}-${idx}`"
            class="sec-cfg-list-row sec-cfg-list-row--readonly"
          >
            <input
              class="sec-cfg-list-input mono"
              :value="entry"
              readonly
              disabled
              :title="t('security.skills.factoryHint')"
            />
            <span class="sec-cfg-list-lock">🔒</span>
          </div>
          <div class="sec-cfg-list-note">
            {{ t("security.skills.factoryNote") }}
          </div>
        </div>
        <div class="sec-cfg-list">
          <div
            v-for="(entry, idx) in draft[field]"
            :key="`${field}-${idx}`"
            class="sec-cfg-list-row"
          >
            <input
              class="sec-cfg-list-input mono"
              :value="entry"
              :placeholder="fieldPlaceholder(field)"
              @input="onUpdate(field, idx, $event)"
            />
            <button
              type="button"
              class="btn btn-ghost btn-sm sec-cfg-list-del"
              :title="t('common.remove')"
              @click="emit('remove-entry', field, idx)"
            >
              ✕
            </button>
          </div>
          <div
            v-if="!draft[field].length"
            class="sec-cfg-list-empty"
          >
            {{ t("security.skills.emptyEntry") }}
          </div>
        </div>
      </div>
    </template>

    <!-- Per-skill run mode. `off` also revokes this skill's path ALLOW. -->
    <div
      v-if="!editing"
      class="sec-cfg-mode-switch"
      :title="t('security.skills.modeHint')"
    >
      <button
        v-for="mode in MODES"
        :key="mode"
        type="button"
        class="sec-cfg-mode-btn"
        :class="{ active: meta.mode === mode }"
        @click="emit('set-mode', skillName, mode)"
      >
        {{ modeLabel(mode) }}
      </button>
    </div>
  </div>
</template>
