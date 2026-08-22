<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CodePersonasPanel — coding-persona system-prompt editor.
 *
 * V1 parity (`CodePersonasPanel.js`): a row-based accordion where each
 * built-in persona is one collapsible row. Expanding a row reveals an
 * inline system-prompt editor with draft/dirty tracking, Save / Cancel /
 * Reset-to-default, an "is_customized" badge, and a Reload action. Only
 * one row is open at a time (accordion); switching away from a row with
 * unsaved edits asks for discard confirmation.
 *
 * V1 is the behaviour source of truth, but the implementation is rebuilt
 * for V2: the panel reuses the global `.cp-*` accordion styles from
 * `settings.css` (no bespoke card grid / undefined classes) and the
 * shared `useConfirm()` custom dialog instead of `window.confirm`.
 *
 * Endpoints (via `useCodePersonas`):
 *   GET    /api/code-personas          → { selected, personas: [...] }
 *   POST   /api/code-personas/:id       { prompt }   (save override)
 *   DELETE /api/code-personas/:id       (reset to built-in default)
 */
import { ref, computed, onMounted, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useCodePersonas, type Persona } from "@/composables/useCodePersonas";
import { useConfirm } from "@/composables/useConfirm";

// ─── State ───────────────────────────────────────────────────────────────────

const { t, locale } = useI18n();
const { confirm } = useConfirm();

/**
 * Localized persona name/description (V1 `useCodePersonas.js:localizedName`
 * / `localizedDescription`): prefer the built-in i18n key
 * (`codePersona.{id}.name` / `.desc`) so the English UI shows English names
 * even when the backend returns the Chinese defaults; fall back to the
 * backend-provided `persona.name` / `description` for any custom persona
 * without an i18n entry.
 */
function localizedName(persona: Persona): string {
  const key = `codePersona.${persona.id}.name`;
  const tr = t(key);
  if (tr && tr !== key) return tr;
  return persona.name || persona.id;
}
function localizedDescription(persona: Persona): string {
  const key = `codePersona.${persona.id}.desc`;
  const tr = t(key);
  if (tr && tr !== key) return tr;
  return persona.description ?? "";
}

const {
  personas,
  loading,
  saving,
  fetchPersonas,
  savePersonaPrompt,
  savePersonaGroups,
  resetPersonaPrompt,
} = useCodePersonas({ t, localizedName, getLocale: () => locale.value });

const search = ref("");

// Accordion: which persona id is expanded (only one) + its draft text.
const expandedId = ref<string | null>(null);
const draftPrompt = ref("");
// Draft groups state: tracks which tool groups are enabled for the expanded persona.
const draftGroups = ref<Record<string, boolean>>({ read: true, edit: true, command: true });

// Tracks whether the initial fetch has completed (V1 `loaded`).
const loaded = ref(false);

// ─── Computed ────────────────────────────────────────────────────────────────

const filteredPersonas = computed(() => {
  const q = search.value.toLowerCase().trim();
  if (!q) return personas.value;
  return personas.value.filter(
    (p) =>
      localizedName(p).toLowerCase().includes(q) ||
      localizedDescription(p).toLowerCase().includes(q),
  );
});

/** Effective prompt for a persona (override falls back to default). */
function effectivePrompt(id: string): string {
  const p = personas.value.find((x) => x.id === id);
  if (!p) return "";
  return p.prompt && p.prompt.length > 0 ? p.prompt : (p.default_prompt ?? "");
}

/** Extract flat group ids from a persona's groups spec. */
function extractGroupIds(groups: Array<string | [string, Record<string, string>]> | undefined): Set<string> {
  const ids = new Set<string>();
  if (!groups) return ids;
  for (const g of groups) {
    if (typeof g === "string") ids.add(g);
    else if (Array.isArray(g) && g.length > 0 && typeof g[0] === "string") ids.add(g[0]);
  }
  return ids;
}

/** Get effective groups for a persona (override falls back to default). */
function effectiveGroups(id: string): Array<string | [string, Record<string, string>]> {
  const p = personas.value.find((x) => x.id === id);
  if (!p) return ["read", "edit", "command"];
  return p.groups ?? p.default_groups ?? ["read", "edit", "command"];
}

/** Build draftGroups record from a persona's groups spec. */
function initDraftGroups(id: string): Record<string, boolean> {
  const ids = extractGroupIds(effectiveGroups(id));
  return { read: ids.has("read"), edit: ids.has("edit"), command: ids.has("command") };
}

/** Check if the file restriction (e.g. .md only) applies to a group for a persona. */
function groupRestriction(id: string, groupId: string): string | null {
  const groups = effectiveGroups(id);
  for (const g of groups) {
    if (Array.isArray(g) && g[0] === groupId && g.length >= 2) {
      const opts = g[1] as Record<string, string>;
      if (opts && opts.fileRegex) return opts.fileRegex;
    }
  }
  return null;
}

/** Check if groups have been modified from the effective state. */
const isGroupsDirty = computed(() => {
  if (expandedId.value === null) return false;
  const currentIds = extractGroupIds(effectiveGroups(expandedId.value));
  return (
    draftGroups.value.read !== currentIds.has("read") ||
    draftGroups.value.edit !== currentIds.has("edit") ||
    draftGroups.value.command !== currentIds.has("command")
  );
});

/** Dirty when the draft differs from the expanded persona's effective prompt. */
const isDirty = computed(() => {
  if (expandedId.value === null) return false;
  return effectivePrompt(expandedId.value) !== draftPrompt.value;
});

// ─── Actions ─────────────────────────────────────────────────────────────────

async function confirmDiscard(): Promise<boolean> {
  return confirm({
    title: t("codePersona.unsavedChanges"),
    message: t("codePersona.discardConfirm"),
    confirmStyle: "danger",
  });
}

async function toggleExpanded(id: string): Promise<void> {
  if (expandedId.value === id) {
    // Collapsing the open row — warn on unsaved changes.
    if ((isDirty.value || isGroupsDirty.value) && !(await confirmDiscard())) return;
    expandedId.value = null;
    return;
  }
  // Opening a new row — warn if the currently open row has unsaved edits.
  if (expandedId.value !== null && (isDirty.value || isGroupsDirty.value) && !(await confirmDiscard())) {
    return;
  }
  draftPrompt.value = effectivePrompt(id);
  draftGroups.value = initDraftGroups(id);
  expandedId.value = id;
}

async function handleSavePrompt(id: string): Promise<void> {
  // Save prompt if dirty.
  if (isDirty.value) {
    const ok = await savePersonaPrompt(id, draftPrompt.value);
    if (ok) draftPrompt.value = effectivePrompt(id);
  }
  // Save groups if dirty.
  if (isGroupsDirty.value) {
    const newGroups: Array<string | [string, Record<string, string>]> = [];
    // Preserve existing restrictions when reconstructing groups.
    const existingGroups = effectiveGroups(id);
    for (const gid of ["read", "edit", "command"]) {
      if (!draftGroups.value[gid]) continue;
      // Check if there was a restriction on this group previously.
      const existing = existingGroups.find(
        (g) => Array.isArray(g) && g[0] === gid,
      );
      if (existing && Array.isArray(existing)) {
        newGroups.push(existing as [string, Record<string, string>]);
      } else {
        newGroups.push(gid);
      }
    }
    await savePersonaGroups(id, newGroups);
    draftGroups.value = initDraftGroups(id);
  }
}

async function cancelEdit(): Promise<void> {
  if ((isDirty.value || isGroupsDirty.value) && !(await confirmDiscard())) return;
  expandedId.value = null;
}

async function handleResetPrompt(id: string): Promise<void> {
  const persona = personas.value.find((p) => p.id === id);
  // Only customized personas can be reset (V1 gating).
  if (!persona || (persona.is_customized === false && persona.is_groups_customized === false)) return;
  const ok = await confirm({
    title: t("codePersona.resetToDefault"),
    message: t("codePersona.resetConfirm", { name: localizedName(persona) }),
    confirmStyle: "danger",
  });
  if (!ok) return;
  const reset = await resetPersonaPrompt(id);
  if (reset && expandedId.value === id) {
    draftPrompt.value = effectivePrompt(id);
    draftGroups.value = initDraftGroups(id);
  }
}

async function handleReload(): Promise<void> {
  if (expandedId.value !== null && (isDirty.value || isGroupsDirty.value) && !(await confirmDiscard())) {
    return;
  }
  expandedId.value = null;
  await fetchPersonas();
}

onMounted(async () => {
  await fetchPersonas();
  loaded.value = true;
});

// Re-fetch when the user switches UI language so the displayed default
// prompts update to match the new locale.
watch(locale, async () => {
  if (!loaded.value) return;
  expandedId.value = null;
  await fetchPersonas();
});
</script>

<template>
  <div class="cp-page">
    <!-- Intro + reload (V1 cp-page-intro) -->
    <div class="cp-page-intro">
      <div class="cp-page-intro-text">
        {{ t("codePersona.settingsDesc") }}
      </div>
      <div class="cp-page-intro-actions">
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="loading"
          :title="t('codePersona.reloadHint')"
          data-testid="persona-reload"
          @click="handleReload"
        >
          <span
            v-if="loading"
            class="spinner"
            style="width: 11px; height: 11px; border-width: 2px; margin-right: 4px"
          ></span>
          <span
            v-else
            style="margin-right: 2px"
          >&#x21BB;</span>
          {{ t("codePersona.reload") }}
        </button>
      </div>
    </div>

    <!-- Search (V2 enhancement) -->
    <div class="config-field">
      <input
        v-model="search"
        type="text"
        class="config-input"
        :placeholder="t('common.search') + '...'"
      />
    </div>

    <!-- Loading placeholder (V1 cp-page-loading) -->
    <div
      v-if="loading && !loaded"
      class="cp-page-loading"
    >
      <span
        class="spinner"
        style="width: 14px; height: 14px; border-width: 2px; margin-right: 8px"
      ></span>
      {{ t("codePersona.loading") }}
    </div>

    <!-- Persona accordion list (V1 cp-page-list) -->
    <div class="cp-page-list">
      <div
        v-for="persona in filteredPersonas"
        :key="persona.id"
        class="cp-row"
      >
        <div
          class="cp-row-header"
          role="button"
          :tabindex="0"
          @click="toggleExpanded(persona.id)"
          @keydown.enter="toggleExpanded(persona.id)"
          @keydown.space.prevent="toggleExpanded(persona.id)"
        >
          <div class="cp-row-title">
            <span class="cp-row-name">{{ localizedName(persona) }}</span>
            <span
              v-if="persona.is_customized"
              class="cp-row-customized"
              :title="t('codePersona.customizedHint')"
              :data-testid="`persona-customized-${persona.id}`"
            >
              {{ t("codePersona.customizedTag") }}
            </span>
          </div>
          <div class="cp-row-desc">
            {{ localizedDescription(persona) }}
          </div>
          <span
            class="collapse-arrow"
            :class="{ collapsed: expandedId !== persona.id }"
          >&#9660;</span>
        </div>

        <div
          v-if="expandedId === persona.id"
          class="cp-row-body"
        >
          <label class="config-label cp-prompt-label">
            {{ t("codePersona.promptLabel") }}
          </label>
          <textarea
            v-model="draftPrompt"
            class="config-input cp-prompt-textarea"
            spellcheck="false"
            wrap="soft"
            :placeholder="t('codePersona.promptPlaceholder')"
            :data-testid="`persona-prompt-${persona.id}`"
          ></textarea>

          <!-- Tool permissions (groups) -->
          <div class="cp-groups-section">
            <label class="config-label cp-groups-label">
              {{ t("codePersona.groups.label") }}
            </label>
            <div class="cp-groups-list">
              <label class="cp-group-item">
                <input
                  v-model="draftGroups.read"
                  type="checkbox"
                  :data-testid="`persona-group-read-${persona.id}`"
                />
                <span class="cp-group-name">{{ t("codePersona.groups.read") }}</span>
                <span class="cp-group-desc">{{ t("codePersona.groups.readDesc") }}</span>
              </label>
              <label class="cp-group-item">
                <input
                  v-model="draftGroups.edit"
                  type="checkbox"
                  :data-testid="`persona-group-edit-${persona.id}`"
                />
                <span class="cp-group-name">{{ t("codePersona.groups.edit") }}</span>
                <span class="cp-group-desc">{{ t("codePersona.groups.editDesc") }}</span>
                <span
                  v-if="groupRestriction(persona.id, 'edit')"
                  class="cp-group-restriction"
                >
                  {{ t("codePersona.groups.restrictedHint", { pattern: groupRestriction(persona.id, "edit") }) }}
                </span>
              </label>
              <label class="cp-group-item">
                <input
                  v-model="draftGroups.command"
                  type="checkbox"
                  :data-testid="`persona-group-command-${persona.id}`"
                />
                <span class="cp-group-name">{{ t("codePersona.groups.command") }}</span>
                <span class="cp-group-desc">{{ t("codePersona.groups.commandDesc") }}</span>
              </label>
            </div>
          </div>

          <div class="cp-row-footer">
            <button
              type="button"
              class="btn btn-primary btn-sm"
              :disabled="(!isDirty && !isGroupsDirty) || saving"
              :data-testid="`persona-save-${persona.id}`"
              @click="handleSavePrompt(persona.id)"
            >
              <span
                v-if="saving"
                class="spinner"
                style="width: 11px; height: 11px; border-width: 2px; margin-right: 4px"
              ></span>
              &#x1F4BE; {{ t("codePersona.save") }}
            </button>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              :disabled="!isDirty && !isGroupsDirty"
              :data-testid="`persona-cancel-${persona.id}`"
              @click="cancelEdit()"
            >
              {{ t("codePersona.cancel") }}
            </button>
            <button
              type="button"
              class="btn btn-ghost btn-sm cp-row-reset"
              :disabled="saving || (persona.is_customized === false && persona.is_groups_customized === false)"
              :title="(persona.is_customized === false && persona.is_groups_customized === false) ? t('codePersona.alreadyDefaultHint') : ''"
              :data-testid="`persona-reset-${persona.id}`"
              @click="handleResetPrompt(persona.id)"
            >
              &#x21BA; {{ t("codePersona.resetToDefault") }}
            </button>
          </div>
          <div
            v-if="isDirty || isGroupsDirty"
            class="cp-row-dirty-hint"
          >
            {{ t("codePersona.unsavedChanges") }}
          </div>
        </div>
      </div>
    </div>

    <!-- Empty state -->
    <div
      v-if="filteredPersonas.length === 0 && loaded"
      class="cp-page-loading"
    >
      {{ t("codePersona.noModesFound") }}
    </div>
  </div>
</template>

<style scoped>
/* Accordion arrow rotation/colour for cp-row headers (settings.css only
   sets layout for `.cp-row-header .collapse-arrow`). */
.cp-row-header .collapse-arrow {
  font-size: var(--text-xs);
  color: var(--text-muted);
  transition: transform 0.2s;
}
.cp-row-header .collapse-arrow.collapsed {
  transform: rotate(-90deg);
}
.cp-prompt-label {
  font-size: var(--text-xs);
  color: var(--text-muted);
}
/* Push reset to the far right within the footer (V1 margin-left:auto). */
.cp-row-reset {
  margin-left: auto;
}
/* Tool permission groups section */
.cp-groups-section {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border-default);
}
.cp-groups-label {
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-bottom: 8px;
}
.cp-groups-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.cp-group-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--text-sm);
  cursor: pointer;
}
.cp-group-item input[type="checkbox"] {
  width: 14px;
  height: 14px;
  cursor: pointer;
}
.cp-group-name {
  font-weight: 500;
  min-width: 80px;
}
.cp-group-desc {
  color: var(--text-muted);
  font-size: var(--text-xs);
}
.cp-group-restriction {
  color: var(--text-warning, #e5a100);
  font-size: var(--text-xs);
  margin-left: 4px;
}
</style>
