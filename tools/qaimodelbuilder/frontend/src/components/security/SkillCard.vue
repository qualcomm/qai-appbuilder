<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * SkillCard — One skill entry in the Security > Skill panel.
 *
 * Renders the V1 ``sec-cfg-skill-card`` shape with two display modes:
 *
 *   - **Read-only summary** (default): icon + label + skill_name +
 *     active badge + edit button + read/write/trusted summary chips.
 *   - **Inline editor**: three list-fields (read / write /
 *     trusted_binaries) with add/remove/update + Save/Cancel.
 *
 * The host parent (``SkillCapabilitiesPanel.vue``) drives the editing
 * state and supplies the draft via the composable; this child stays
 * presentational so the panel template stays under the .vue cohesion
 * budget (AGENTS.md need A: ≤600 lines).
 *
 * Three visual variants are supported by ``variant`` prop:
 *   - "feature": built-in capability (custom icon + i18n label).
 *   - "agent":   user-installed agent skill (⚡ icon + skill_name).
 *   - "agent-empty-row": agent skill with no policy yet — compact row
 *     when not editing, full editor when ``editing``.
 */
import { useI18n } from "vue-i18n";

import {
  FEATURE_ICONS,
  type DiscoveredSkillEntry,
  type SkillDraftField,
  type SkillMode,
} from "@/composables/useSkillCapabilities";

const props = defineProps<{
  skillName: string;
  meta: DiscoveredSkillEntry;
  variant: "feature" | "agent" | "agent-empty-row";
  editing: boolean;
  saving: boolean;
  draft: { read: string[]; write: string[]; trusted_binaries: string[] };
}>();

const emit = defineEmits<{
  (e: "start-edit", name: string, meta: DiscoveredSkillEntry): void;
  (e: "cancel"): void;
  (e: "save", name: string): void;
  (e: "add-entry", field: SkillDraftField): void;
  (e: "remove-entry", field: SkillDraftField, idx: number): void;
  (e: "update-entry", field: SkillDraftField, idx: number, val: string): void;
  (e: "set-mode", name: string, mode: SkillMode): void;
}>();

const { t } = useI18n();

// FEATURE_META parity (icon + i18n label).
const FEATURE_NAMES = new Set([
  "model-builder",
  "ppt-gen",
  "code-assist",
  "translate",
]);
function featureLabel(name: string): string {
  return FEATURE_NAMES.has(name) ? t(`security.featureMeta.${name}`) : name;
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

// V2 mode switch enhancement.
const MODES: readonly SkillMode[] = ["off", "local", "cloud", "both"] as const;
function modeLabel(mode: string | undefined): string {
  switch (mode) {
    case "off":
      return "Off";
    case "cloud":
      return "Cloud";
    case "local":
      return "Local";
    case "both":
      return "Both";
    default:
      return "Auto";
  }
}

function onUpdate(
  field: SkillDraftField,
  idx: number,
  ev: Event,
): void {
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
      variant === 'agent-empty-row' && !editing && 'sec-cfg-skill-no-policy',
    ]"
  >
    <!-- agent-empty-row: compact row when not editing -->
    <template
      v-if="variant === 'agent-empty-row' && !editing"
    >
      <span class="sec-cfg-skill-ficon">⚡</span>
      <span class="sec-cfg-skill-fid mono">{{ skillName }}</span>
      <button
        type="button"
        class="btn btn-ghost btn-sm sec-cfg-skill-add-policy-btn"
        @click="emit('start-edit', skillName, meta)"
      >
        {{ t("security.skills.addPolicyBtn") }}
      </button>
    </template>

    <!-- All other states: full card / editor -->
    <template v-else>
      <div class="sec-cfg-skill-feature-meta">
        <template v-if="variant === 'feature'">
          <span class="sec-cfg-skill-ficon">{{ featureIcon(skillName) }}</span>
          <span class="sec-cfg-skill-flabel">{{ featureLabel(skillName) }}</span>
          <span class="sec-cfg-skill-fid mono">{{ skillName }}</span>
        </template>
        <template v-else>
          <span class="sec-cfg-skill-ficon">⚡</span>
          <span class="sec-cfg-skill-fid mono">{{ skillName }}</span>
          <span
            v-if="variant === 'agent-empty-row'"
            class="sec-cfg-skill-no-policy-hint"
          >
            {{ t("security.skills.creating") }}
          </span>
        </template>
        <span
          v-if="meta.active && variant !== 'agent-empty-row'"
          class="sec-cfg-skill-badge sec-cfg-skill-badge--active"
        >
          {{ t("security.skills.activeLabel") }}
        </span>
        <div class="sec-cfg-skill-card-actions">
          <template v-if="!editing">
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              @click="emit('start-edit', skillName, meta)"
            >
              {{
                meta.has_policy
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
                variant === "agent-empty-row"
                  ? t("security.skills.createBtn")
                  : t("security.skills.saveBtn")
              }}
            </button>
          </template>
        </div>
      </div>

      <!-- Read-only summary -->
      <div
        v-if="!editing"
        class="sec-cfg-skill-summary"
      >
        <span v-if="meta.read.length">
          {{ t("security.skills.readSummary", { n: meta.read.length }) }}
        </span>
        <span v-if="meta.write.length">
          {{ t("security.skills.writeSummary", { n: meta.write.length }) }}
        </span>
        <span v-if="meta.trusted_binaries.length">
          {{
            t("security.skills.trustedSummary", {
              n: meta.trusted_binaries.length,
            })
          }}
        </span>
        <span
          v-if="
            !meta.read.length &&
              !meta.write.length &&
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

      <!-- V2 enhancement: per-skill mode switch (only on full card) -->
      <div
        v-if="!editing && variant !== 'agent-empty-row'"
        class="sec-cfg-mode-switch"
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
    </template>
  </div>
</template>
