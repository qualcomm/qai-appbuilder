<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ProjectAccessPanel — project directory access control.
 *
 * V1 parity (`components/ProjectAccessPanel.js` + `composables/useProjectAccess.js`):
 * a single project root (`enabled` / `path`) edited as a local draft, saved
 * explicitly. Regions: status overview + enable switch, disabled-warning
 * banner, project-path input, save/cancel actions, loading + error overlays.
 *
 * V2 structure notes (designed > V1):
 *   - Server state + API live in `useProjectAccess` composable; this component
 *     only owns the editable draft + UI-local state (V1 mixed both in setup).
 *   - Toggle confirmations use the global `useConfirm()` custom dialog
 *     (§3.9 — no native confirm/alert), replacing V1's bespoke per-panel
 *     confirm overlay. The transient save banner replaces V1's inline timer.
 *   - Reuses the global shared `sec-*` CSS classes shared with the sibling
 *     panels.
 */
import { reactive, ref, computed, onMounted, onBeforeUnmount, watch } from "vue";
import { useI18n } from "vue-i18n";

import { useConfirm } from "@/composables/useConfirm";
import {
  useProjectAccess,
  type ProjectAccessStatus,
} from "@/composables/useProjectAccess";

const props = withDefaults(defineProps<{ visible?: boolean }>(), {
  visible: true,
});

const { t } = useI18n();
const { confirm } = useConfirm();

const { status, loading, saving, lastError, fetchStatus, updateStatus } =
  useProjectAccess();

// ─── Local editable draft (mirrors V1 `draft`) ─────────────────────────────────

const draft = reactive<ProjectAccessStatus>({
  enabled: false,
  path: "",
});

function syncDraft(): void {
  draft.enabled = status.enabled;
  draft.path = status.path;
}

const hasUnsavedChanges = computed(
  () => draft.enabled !== status.enabled || draft.path !== status.path,
);

// ─── Transient save banner (replaces V1 inline timer) ──────────────────────────

const saveStatus = ref<{ type: "success" | "error"; message: string } | null>(
  null,
);
let statusTimer: ReturnType<typeof setTimeout> | null = null;

function showStatus(type: "success" | "error", message: string): void {
  saveStatus.value = { type, message };
  if (statusTimer) clearTimeout(statusTimer);
  statusTimer = setTimeout(() => {
    saveStatus.value = null;
  }, 3500);
}

// ─── Actions ───────────────────────────────────────────────────────────────────

async function handleToggle(enabled: boolean): Promise<void> {
  // V1: only confirm when a path is configured.
  if (!enabled && draft.path) {
    const ok = await confirm({
      icon: "⚠️",
      title: t("projectAccess.dialogs.disableTitle"),
      message: t("projectAccess.confirmDisable"),
      confirmStyle: "danger",
      confirmText: t("common.confirm"),
      cancelText: t("common.cancel"),
    });
    if (!ok) return;
  } else if (enabled && draft.path) {
    const ok = await confirm({
      icon: "ℹ️",
      title: t("projectAccess.dialogs.enableTitle"),
      message: t("projectAccess.confirmEnable", { path: draft.path }),
      confirmStyle: "primary",
      confirmText: t("common.confirm"),
      cancelText: t("common.cancel"),
    });
    if (!ok) return;
  }
  draft.enabled = enabled;
}

async function handleSave(): Promise<void> {
  try {
    await updateStatus({
      enabled: draft.enabled,
      path: draft.path,
    });
    syncDraft();
    showStatus("success", t("projectAccess.notifications.saved"));
  } catch (e) {
    showStatus(
      "error",
      `${t("projectAccess.notifications.saveFailed")}: ${(e as Error).message}`,
    );
  }
}


// ─── Lifecycle ─────────────────────────────────────────────────────────────────

onMounted(async () => {
  await fetchStatus();
  syncDraft();
});

onBeforeUnmount(() => {
  if (statusTimer) clearTimeout(statusTimer);
});

watch(
  () => props.visible,
  (val) => {
    if (val) void fetchStatus().then(syncDraft);
  },
);
</script>

<template>
  <div
    v-show="visible"
    class="project-access-panel sec-config-panel"
    data-testid="project-access-panel"
  >
    <!-- ── Status overview ───────────────────────────────────────────────── -->
    <section class="sec-section sec-overview">
      <div class="sec-overview-header">
        <div class="sec-overview-status">
          <span
            class="sec-status-dot"
            :class="draft.enabled ? 'sec-status-dot--green' : 'sec-status-dot--gray'"
          />
          <span class="sec-status-label">
            {{ draft.enabled ? t("projectAccess.status.enabled") : t("projectAccess.status.disabled") }}
          </span>
        </div>
      </div>
      <div class="sec-overview-toggle">
        <label class="sec-switch">
          <input
            type="checkbox"
            :checked="draft.enabled"
            :disabled="saving"
            data-testid="project-access-toggle"
            @change="handleToggle(($event.target as HTMLInputElement).checked)"
          />
          <span class="sec-switch-slider" />
        </label>
        <span class="sec-switch-text">
          {{ draft.enabled ? t("projectAccess.enableLabel") : t("projectAccess.disableLabel") }}
        </span>
      </div>
    </section>

    <!-- ── Disabled warning banner ───────────────────────────────────────── -->
    <div
      v-if="!draft.enabled"
      class="sec-section pacl-warning-section"
    >
      <div class="pacl-warning">
        <span>⚠️</span>
        <span>{{ t("projectAccess.disabledWarning") }}</span>
      </div>
    </div>

    <!-- ── Project path ──────────────────────────────────────────────────── -->
    <section
      class="sec-section"
      :class="{ 'sec-section--disabled': !draft.enabled }"
    >
      <h3 class="sec-section-title">
        {{ t("projectAccess.pathLabel") }}
      </h3>
      <div class="sec-field">
        <input
          v-model="draft.path"
          type="text"
          class="sec-input mono"
          :disabled="!draft.enabled || saving"
          :placeholder="t('projectAccess.pathPlaceholder')"
          data-testid="project-access-path"
        />
        <p class="sec-field-desc">
          {{ t("projectAccess.pathHint") }}
        </p>
      </div>
    </section>

    <!-- ── Actions ───────────────────────────────────────────────────────── -->
    <section class="sec-section sec-actions">
      <div class="sec-actions-row">
        <button
          type="button"
          class="btn btn-primary"
          :disabled="saving || !hasUnsavedChanges"
          data-testid="project-access-save"
          @click="handleSave"
        >
          <span
            v-if="saving"
            class="spinner pacl-spinner"
          />
          <span v-else>💾</span>
          {{ t("common.save") }}
        </button>
        <button
          type="button"
          class="btn btn-ghost"
          :disabled="saving || !hasUnsavedChanges"
          data-testid="project-access-cancel"
          @click="syncDraft"
        >
          ↺ {{ t("common.cancel") }}
        </button>
      </div>

      <div
        v-if="saveStatus"
        class="sec-save-status"
        :class="`sec-save-status--${saveStatus.type}`"
      >
        {{ saveStatus.message }}
      </div>
    </section>

    <!-- ── Loading overlay ───────────────────────────────────────────────── -->
    <div
      v-if="loading"
      class="sec-loading-overlay"
    >
      <span class="spinner" />
    </div>

    <!-- ── Error banner ──────────────────────────────────────────────────── -->
    <div
      v-if="lastError"
      class="sec-error-banner"
      data-testid="project-access-error"
      @click="lastError = null"
    >
      ⚠️ {{ lastError }}
    </div>
  </div>
</template>

<style scoped>
.pacl-warning-section {
  padding: 10px 14px;
}

.pacl-warning {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: 10px 12px;
  background: rgba(251, 191, 36, 0.08);
  border: 1px solid rgba(251, 191, 36, 0.25);
  border-radius: 6px;
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.pacl-tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
  min-height: 32px;
}

.pacl-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: var(--text-xs);
  font-family: var(--font-mono);
  background: var(--accent-muted);
  border: 1px solid var(--accent);
  color: var(--text-primary);
}

.pacl-tag-remove {
  background: none;
  border: none;
  cursor: pointer;
  padding: 0 2px;
  color: var(--text-muted);
  font-size: 14px;
  line-height: 1;
}

.pacl-tag-remove:disabled {
  cursor: default;
  opacity: 0.5;
}

.pacl-tag-empty {
  font-size: var(--text-xs);
  color: var(--text-muted);
  padding: 4px 0;
}

.pacl-add-row {
  margin-bottom: 0;
}

.pacl-add-field {
  flex: 1;
  gap: 6px;
}

.pacl-add-input {
  flex: 1;
}

.pacl-spinner {
  width: 12px;
  height: 12px;
  border-width: 2px;
}
</style>
