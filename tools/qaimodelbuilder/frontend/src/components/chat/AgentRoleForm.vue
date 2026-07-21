<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Shared role-editing form (display name / model / persona / tools / colour).
 *
 * Extracted from the DiscussionPanel inline "+ 添加 Agent" editor so the main
 * panel AND the AgentTemplateDialog "新建/编辑角色" form share ONE field set —
 * killing the previous field drift (问题 7). The host owns nothing about tool
 * catalog loading or colour palette; this component carries it all and emits a
 * single `RoleFormData` object via `v-model:value`.
 *
 * Decision 2 (chip dimming): when `currentModePolicy` denies a tool, its chip
 * is greyed + tooltip'd — but stays selectable (the role keeps its persona; the
 * tool becomes live again under a permissive mode). `currentModePolicy = null`
 * (no mode selected, or the template version where the future conversation is
 * unknown — §7.16) ⇒ never dim.
 *
 * The component does NOT impose default tool selection — the CALLER seeds the
 * `value` initial object (main panel: default-on; template edit: persisted
 * value; §7.9 / §7.17). Empty `allowedTools` is honoured verbatim.
 */
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { fetchChatTools, type ChatToolDescriptor } from "@/api/chatTools";
import { useCloudModelOptions } from "@/composables/chat/useCloudModelOptions";
import { useSkillsStore } from "@/stores/skills";
import {
  discussionColorToken,
  DISCUSSION_PALETTE_SIZE,
} from "@/stores/_chatTabsTypes";
import { isAdvertised, type ModeToolPolicy } from "@/lib/modePolicy";
import ToolGroupToggle from "./ToolGroupToggle.vue";

/** The unified editable shape this form binds to (v-model:value). */
export interface RoleFormData {
  displayName: string;
  modelId: string;
  persona: string;
  allowedTools: string[];
  /** Skill ids (SKILL.md parent-dir name) this role may use — the per-role skill
   *  whitelist (backend `config.enabled_skills`). Empty ⇒ the role has no skill
   *  (the default). Only globally-enabled skills are selectable in the UI. */
  enabledSkills: string[];
  color: number;
}

const props = withDefaults(
  defineProps<{
    value: RoleFormData;
    /** Selected mode's tool policy for chip dimming; null = never dim (§7.16). */
    currentModePolicy?: ModeToolPolicy | null;
    /** Mode name for the dim tooltip. */
    currentModeName?: string | null;
  }>(),
  {
    currentModePolicy: null,
    currentModeName: null,
  },
);

const emit = defineEmits<{
  (e: "update:value", value: RoleFormData): void;
}>();

const { t } = useI18n();

const skillsStore = useSkillsStore();

/** The `skill` meta-tool is NOT selectable as an ordinary tool chip anymore —
 *  a role's skills are chosen in the dedicated SKILL multi-select below (bound
 *  to `enabledSkills`), and the backend derives the skill tool from a non-empty
 *  `enabled_skills`. Excluded from BOTH the catalog and the fallback set. */
const SKILL_TOOL_NAME = "skill";

// ── Same fallback / default-off rules the main panel used (1:1 extraction) ──
const FALLBACK_TOOLS: readonly string[] = [
  "read",
  "edit",
  "write",
  "exec",
  "glob",
  "grep",
  "webfetch",
  "agent",
  "todowrite",
  "question",
] as const;

const toolCatalog = ref<ChatToolDescriptor[]>([]);
const toolCatalogLoaded = ref(false);

const SELECTABLE_TOOLS = computed<string[]>(() =>
  (toolCatalogLoaded.value && toolCatalog.value.length > 0
    ? toolCatalog.value.map((tldesc) => tldesc.name)
    : [...FALLBACK_TOOLS]
  ).filter((name) => name !== SKILL_TOOL_NAME),
);

async function loadToolCatalog(): Promise<void> {
  if (toolCatalogLoaded.value) return;
  try {
    const res = await fetchChatTools();
    toolCatalog.value = Array.isArray(res?.tools)
      ? res.tools.filter((tldesc) => tldesc.available_in_discussion)
      : [];
  } catch {
    toolCatalog.value = [];
  } finally {
    toolCatalogLoaded.value = true;
  }
}

/** Globally-enabled skills (Settings mode !== 'off') — the pool a role may pick
 *  from. Default selection is empty (the caller seeds `enabledSkills: []`). */
const selectableSkills = computed(() => skillsStore.enabledSkills);

// ── Cloud-model dropdown (shared composable; local models excluded) ─────────
const {
  cloudModelOptions,
  cloudModelLabel,
  loadCloudModels,
  modelMissing: isModelMissing,
} = useCloudModelOptions();

const modelMissing = computed(() => isModelMissing(props.value.modelId));

/** Required-field hint: the role model is mandatory now that "Default" is gone
 *  (cloud-only, must be picked). Shown when the bound modelId is still empty. */
const modelRequired = computed(() => props.value.modelId.trim() === "");

onMounted(() => {
  void loadCloudModels();
  void loadToolCatalog();
  // The SKILL multi-select pool = globally-enabled skills (idempotent fetch).
  void skillsStore.ensureLoaded();
});

// ── v-model plumbing — emit a fresh object on each field edit ───────────────
function patch(partial: Partial<RoleFormData>): void {
  emit("update:value", { ...props.value, ...partial });
}

const paletteIndices = computed(() =>
  Array.from({ length: DISCUSSION_PALETTE_SIZE }, (_, i) => i),
);

const allToolsSelected = computed(() =>
  SELECTABLE_TOOLS.value.every((tool) => props.value.allowedTools.includes(tool)),
);

function toggleAllTools(): void {
  patch({
    allowedTools: allToolsSelected.value ? [] : [...SELECTABLE_TOOLS.value],
  });
}

// ── Per-role SKILL whitelist (independent of the tool grid) ─────────────────
function toggleSkill(id: string): void {
  const next = [...props.value.enabledSkills];
  const i = next.indexOf(id);
  if (i >= 0) next.splice(i, 1);
  else next.push(id);
  patch({ enabledSkills: next });
}

// ── Chip dimming (decision 2) ───────────────────────────────────────────────
function isToolDimmed(tool: string): boolean {
  if (!props.currentModePolicy) return false;
  return !isAdvertised(props.currentModePolicy, tool);
}

function dimTooltip(tool: string): string {
  if (!isToolDimmed(tool)) return "";
  return t("chat.discussion.roleForm.toolDimmed", {
    mode: props.currentModeName ?? "",
  });
}

// Keep the model dropdown populated when the caller swaps in a value with a
// preset modelId (edit flow) after mount.
watch(
  () => props.value.modelId,
  () => {
    void loadCloudModels();
  },
);
</script>

<template>
  <div
    class="role-form"
    data-testid="agent-role-form"
  >
    <label class="role-field">
      <span>{{ t("chat.discussion.displayName") }}</span>
      <input
        :value="value.displayName"
        type="text"
        data-testid="role-form-name"
        :placeholder="t('chat.discussion.displayNamePlaceholder')"
        @input="
          patch({ displayName: ($event.target as HTMLInputElement).value })
        "
      />
    </label>

    <label class="role-field">
      <span>{{ t("chat.discussion.modelId") }}</span>
      <select
        :value="value.modelId"
        class="role-model-select"
        data-testid="role-form-model"
        @change="patch({ modelId: ($event.target as HTMLSelectElement).value })"
      >
        <option
          v-for="m in cloudModelOptions"
          :key="m.model_id"
          :value="m.model_id"
        >
          {{ cloudModelLabel(m) }}
        </option>
        <option
          v-if="modelMissing"
          :value="value.modelId"
          data-testid="role-form-model-legacy"
        >
          {{ value.modelId }}
        </option>
      </select>
      <span
        v-if="modelRequired"
        class="role-field-required"
        data-testid="role-form-model-required"
      >
        {{ t("chat.discussion.modelRequired") }}
      </span>
    </label>

    <label class="role-field">
      <span>{{ t("chat.discussion.persona") }}</span>
      <textarea
        :value="value.persona"
        rows="3"
        data-testid="role-form-persona"
        :placeholder="t('chat.discussion.personaPlaceholder')"
        @input="patch({ persona: ($event.target as HTMLTextAreaElement).value })"
      ></textarea>
    </label>

    <!-- Per-role tool set -->
    <div class="role-field">
      <span class="role-tools-header">
        {{ t("chat.discussion.allowedTools") }}
        <button
          type="button"
          class="role-tools-selectall"
          data-testid="role-form-tools-select-all"
          @click="toggleAllTools"
        >
          {{
            allToolsSelected
              ? t("chat.discussion.clearAllTools")
              : t("chat.discussion.selectAllTools")
          }}
        </button>
      </span>
      <ToolGroupToggle
        :model-value="value.allowedTools"
        :tools="SELECTABLE_TOOLS"
        mode="allowed"
        :is-dimmed="isToolDimmed"
        :dim-tooltip="dimTooltip"
        @update:model-value="patch({ allowedTools: $event })"
      />
    </div>

    <!-- Per-role SKILL whitelist (independent multi-select; pool = globally
         enabled skills). Empty selection ⇒ the role has no skill (default). -->
    <div class="role-field">
      <span class="role-tools-header">
        {{ t("chat.discussion.enabledSkills") }}
      </span>
      <p
        v-if="selectableSkills.length === 0"
        class="role-skills-empty"
        data-testid="role-form-skills-empty"
      >
        {{ t("chat.discussion.enabledSkillsEmpty") }}
      </p>
      <div v-else class="role-tools-grid">
        <label
          v-for="sk in selectableSkills"
          :key="sk.id"
          class="role-tool-chip"
          :class="{ 'is-on': value.enabledSkills.includes(sk.id) }"
          :title="sk.description || sk.name"
        >
          <input
            type="checkbox"
            :checked="value.enabledSkills.includes(sk.id)"
            :data-testid="`role-form-skill-${sk.id}`"
            @change="toggleSkill(sk.id)"
          />
          {{ sk.name }}
        </label>
      </div>
    </div>

    <!-- Colour picker (theme-aware palette indices) -->
    <div class="role-field">
      <span>{{ t("chat.discussion.color") }}</span>
      <div class="role-color-grid">
        <button
          v-for="i in paletteIndices"
          :key="i"
          type="button"
          class="role-color-swatch"
          :class="{ 'is-selected': value.color === i }"
          :style="{ background: discussionColorToken(i) }"
          :data-testid="`role-form-color-${i}`"
          :aria-label="t('chat.discussion.color') + ' ' + (i + 1)"
          @click="patch({ color: i })"
        ></button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.role-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.role-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.role-field input,
.role-field textarea {
  padding: var(--space-2);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font: inherit;
  resize: vertical;
}
.role-model-select {
  padding: var(--space-2);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font: inherit;
  cursor: pointer;
}
.role-field-required {
  font-size: var(--text-xs);
  color: var(--error);
}
.role-tools-grid {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}
.role-tools-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.role-tools-selectall {
  background: transparent;
  border: none;
  color: var(--accent);
  cursor: pointer;
  font-size: var(--text-xs);
  padding: 0;
}
.role-tools-selectall:hover {
  text-decoration: underline;
}
.role-tool-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px var(--space-2);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  font-size: var(--text-xs);
  cursor: pointer;
}
.role-tool-chip.is-on {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-muted);
}
.role-tool-chip.is-dimmed {
  opacity: 0.45;
}
.role-skills-empty {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--text-secondary);
}
.role-color-grid {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}
.role-color-swatch {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 2px solid transparent;
  cursor: pointer;
}
.role-color-swatch.is-selected {
  border-color: var(--text-primary);
}
</style>
