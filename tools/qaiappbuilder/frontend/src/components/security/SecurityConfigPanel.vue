<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * SecurityConfigPanel — V1 "Allow Lists" tab (4 path/command categories).
 *
 * Restores the V1 SecurityConfigPanel.js Tab 2 layout: four category blocks
 * (read_allow / write_allow / exec_allow_cwd / exec_deny_patterns) each with
 * a title + mono field key, description, ＋Add, editable rows with ✕ delete
 * and an empty-state, plus a unified 💾 Save / ↺ Reset bar with an unsaved
 * indicator.
 *
 * The flat V1 lists are projected onto V2's rules-based Policy (which carries
 * an explicit `op` dimension) by the `usePolicyLists` composable, so this
 * component stays a thin view and the projection/fold-back logic is testable
 * in isolation. Uses global CSS from security.css (`.sec-cfg-list-*`).
 */
import { onMounted } from "vue";
import { useI18n } from "vue-i18n";

import PolicyListBlock from "@/components/security/PolicyListBlock.vue";
import { useConfirm } from "@/composables/useConfirm";
import { useReboot } from "@/composables/useReboot";
import {
  LIST_FIELDS,
  usePolicyLists,
  type ListField,
} from "@/composables/usePolicyLists";
import { useToastStore } from "@/stores/toast";

const { t } = useI18n();
const toast = useToastStore();
const { confirm } = useConfirm();
const { requestRebootDirect } = useReboot();

const {
  loading,
  saving,
  readOnly,
  version,
  saveStatus,
  lastError,
  draft,
  hasUnsavedChanges,
  load,
  addEntry,
  updateEntry,
  removeEntry,
  reset,
  save,
} = usePolicyLists();

// ─── i18n maps (V1 SecurityConfigPanel.js:637-658 parity) ────────────────────

const TITLE_KEY: Record<ListField, string> = {
  read_allow: "security.listReadablePaths",
  write_allow: "security.listWritablePaths",
  write_deny: "security.pathBlocklistTitle",
  exec_allow_cwd: "security.listExecScope",
  exec_deny_patterns: "security.listBlockedPatterns",
};

const DESC_KEY: Record<ListField, string> = {
  read_allow: "security.listReadablePathsDesc",
  write_allow: "security.listWritablePathsDesc",
  write_deny: "security.pathBlocklistHint",
  exec_allow_cwd: "security.listExecScopeDesc",
  exec_deny_patterns: "security.listBlockedPatternsDesc",
};

function fieldTitle(field: ListField): string {
  return t(TITLE_KEY[field]);
}

function fieldDesc(field: ListField): string {
  return t(DESC_KEY[field]);
}

function fieldHint(field: ListField): string {
  // exec_deny_patterns are regexes; write_deny is a path glob with its own
  // example placeholder; everything else is a plain path (V1:646-648).
  if (field === "exec_deny_patterns") return t("security.listHintRegex");
  if (field === "write_deny") return t("security.pathBlocklistPlaceholder");
  return t("security.listHintPath");
}

async function onSave(): Promise<void> {
  const { ok, needsReboot } = await save();
  if (!ok && lastError.value === "read-only") {
    toast.push({
      id: crypto.randomUUID(),
      kind: "warning",
      message: t("security.config.readOnlyToast"),
      timeoutMs: 5000,
    });
  } else if (!ok && lastError.value) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: t("security.config.saveFailed", { msg: lastError.value }),
      timeoutMs: 5000,
    });
  } else if (ok && needsReboot) {
    const accepted = await confirm({
      icon: "🔄",
      title: t("security.config.rebootTitle"),
      message: t("security.config.rebootMessage"),
      confirmText: t("security.config.rebootConfirm"),
      cancelText: t("security.config.rebootCancel"),
      confirmStyle: "primary",
    });
    if (accepted) {
      await requestRebootDirect();
    } else {
      toast.push({
        id: crypto.randomUUID(),
        kind: "info",
        message: t("security.config.rebootDeferred"),
        timeoutMs: 5000,
      });
    }
  }
}

onMounted(() => {
  void load();
});

/**
 * Expose unsaved-state and save-status so SecurityView can show the
 * header-level "unsaved" hint and saveStatus message (V1 parity:
 * SecurityConfigPanel.js:959-961, 968-970).
 */
defineExpose({ hasUnsavedChanges, saveStatus });
</script>

<template>
  <div
    class="security-section"
    data-testid="security-config-panel"
  >
    <!-- Header -->
    <div class="sec-cfg-block-header">
      <span class="sec-cfg-block-title">{{ t("security.config.title") }}</span>
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        :disabled="loading"
        @click="load"
      >
        {{ t("security.config.refresh") }}
      </button>
    </div>

    <!-- Read-only notice -->
    <div
      v-if="readOnly"
      class="sec-cfg-health-banner sec-cfg-health-banner--warn"
    >
      <span class="sec-cfg-health-icon">⚠️</span>
      <div class="sec-cfg-health-text">
        {{ t("security.config.readOnlyBanner") }}
      </div>
    </div>

    <!-- Loading -->
    <p
      v-if="loading"
      class="config-comment"
    >
      {{ t("security.config.loading") }}
    </p>

    <template v-else>
      <!-- 4 category blocks (V1 read_allow / write_allow / exec_allow_cwd / exec_deny_patterns) -->
      <PolicyListBlock
        v-for="field in LIST_FIELDS"
        :key="field"
        :field="field"
        :title="fieldTitle(field)"
        :desc="fieldDesc(field)"
        :hint="fieldHint(field)"
        :entries="draft[field]"
        :add-label="t('common.add')"
        :empty-label="t('security.listEmpty')"
        :disabled="readOnly"
        @add="addEntry(field)"
        @update="(idx, value) => updateEntry(field, idx, value)"
        @remove="(idx) => removeEntry(field, idx)"
      />

      <!-- Unified save bar (V1 SecurityConfigPanel.js:1413-1422) -->
      <div
        v-if="!readOnly"
        class="sec-cfg-save-bar"
      >
        <button
          type="button"
          class="btn btn-primary"
          :disabled="saving || !hasUnsavedChanges"
          @click="onSave"
        >
          <span v-if="saving">{{ t("security.config.saving") }}</span>
          <span v-else>💾 {{ t("security.saveListsBtn") }}</span>
        </button>
        <button
          type="button"
          class="btn btn-ghost"
          :disabled="!hasUnsavedChanges"
          @click="reset"
        >
          ↺ {{ t("common.reset") }}
        </button>
        <span
          v-if="saveStatus === 'success'"
          class="sec-cfg-status sec-cfg-status--success"
        >
          {{ t("security.config.saveStatusSuccess") }}
        </span>
        <span
          v-else-if="saveStatus === 'error'"
          class="sec-cfg-status sec-cfg-status--error"
        >
          {{ t("security.config.saveStatusError") }}
        </span>
        <span
          v-else-if="hasUnsavedChanges"
          class="sec-cfg-unsaved"
        >
          {{ t("security.unsavedChanges") }}
        </span>
        <span class="config-comment">
          {{ t("security.config.versionLabel", { n: version }) }}
        </span>
      </div>
    </template>
  </div>
</template>
