<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ImplementationDrawer — right-side drawer hosting the full implementation
 * plan management UI. Replaces the old inline ImplementationPanel.
 *
 * Pattern: Teleport-to-body overlay (same as GomasterOptimizeDrawer).
 * Non-modal while implementing (pointer-events pass through to chat).
 *
 * Contains:
 * - Header with phase label + progress + close button
 * - Generate Plan CTA (when phase === "none")
 * - Controls (Start / Pause / Resume / Stop / Retry)
 * - Item list with inline editing (role, skip, delete)
 * - Add item form
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useConfirm } from "@/composables/useConfirm";
import { useImplementation } from "@/composables/chat/useImplementation";
import { useDiscussion } from "@/composables/chat/useDiscussion";
import type { ImplementationItemVM } from "@/stores/_chatTabsTypes";

const props = defineProps<{
  open: boolean;
}>();

const emit = defineEmits<{
  (e: "close"): void;
}>();

const { t } = useI18n();
const { confirm } = useConfirm();
const impl = useImplementation();
const discussion = useDiscussion();

const phase = impl.phase;
const items = impl.items;
const currentItem = impl.currentItem;

/** Role roster from the discussion config. */
const roster = computed(() => discussion.participants.value);
const roleOptions = computed(() => [
  { value: "", label: t("chat.implementation.item.roleUnassigned") },
  ...roster.value.map((p) => ({ value: p.display_name, label: p.display_name })),
]);

// -- Generate Plan --
/** Whether generate is possible (participants with model + discussion happened). */
const canGenerate = computed(() => {
  const r = roster.value;
  return r.length > 0 && r.some((p) => p.model_id !== undefined && p.model_id !== "");
});
const generating = ref(false);
async function onGenerate(): Promise<void> {
  generating.value = true;
  try {
    await impl.generatePlan();
  } finally {
    generating.value = false;
  }
}

// -- Execution controls --
const controlBusy = ref(false);
async function onStart(): Promise<void> {
  controlBusy.value = true;
  try { await impl.start(); } finally { controlBusy.value = false; }
}
async function onPause(): Promise<void> {
  controlBusy.value = true;
  try { await impl.pause(); } finally { controlBusy.value = false; }
}
async function onResume(): Promise<void> {
  controlBusy.value = true;
  try { await impl.resume(); } finally { controlBusy.value = false; }
}
async function onStop(): Promise<void> {
  controlBusy.value = true;
  try { await impl.stop(); } finally { controlBusy.value = false; }
}
async function onRetry(): Promise<void> {
  controlBusy.value = true;
  try { await impl.retry(); } finally { controlBusy.value = false; }
}

// -- Item actions --
async function onRoleChange(item: ImplementationItemVM, role: string): Promise<void> {
  await impl.updateItem(item.id, { assignedRole: role || null });
}
async function onSkip(item: ImplementationItemVM): Promise<void> {
  await impl.skipItem(item.id);
}
async function onDelete(item: ImplementationItemVM): Promise<void> {
  const ok = await confirm({
    title: t("chat.implementation.confirmDelete.title"),
    message: t("chat.implementation.confirmDelete.message", { title: item.title }),
  });
  if (ok) await impl.deleteItem(item.id);
}

// -- Add item --
const newItemTitle = ref("");
async function onAddItem(): Promise<void> {
  const title = newItemTitle.value.trim();
  if (!title) return;
  await impl.addItem({ title });
  newItemTitle.value = "";
}

// -- Plan document affordance --
// The coordinator writes the full plan to a workspace markdown file; lightweight
// items only carry a title + auto-assigned role, with the detail living in the
// doc. We surface the doc name so the user can open it (or at least see where
// the detail lives). Opening a workspace file by name is not wired end-to-end,
// so this surfaces the localized plan-doc name as the affordance label.
const planDocName = computed(() => t("chat.implementation.planDocName"));
const openPlanDocLabel = computed(() =>
  t("chat.implementation.openPlanDoc", { name: planDocName.value }),
);
const docDetailHint = computed(() =>
  t("chat.implementation.docDetailHint", { name: planDocName.value }),
);

// -- Expanded item details --
const expandedItemId = ref<string | null>(null);
function toggleDetails(id: string): void {
  expandedItemId.value = expandedItemId.value === id ? null : id;
}

/** Localized label for an item's auto-assigned role (or "unassigned"). */
function assignedRoleLabel(item: ImplementationItemVM): string {
  return item.assignedRole ?? t("chat.implementation.assignedRoleNone");
}

// -- Progress --
const doneCount = computed(
  () => items.value.filter((x) => x.status === "done").length,
);
const failedCount = computed(
  () => items.value.filter((x) => x.status === "failed").length,
);

// -- Drawer close via overlay click --
const pointerDownOnOverlay = ref(false);
function onOverlayPointerDown(event: PointerEvent): void {
  if (isRunning.value) return;
  pointerDownOnOverlay.value = event.target === event.currentTarget;
}
function onOverlayClick(event: MouseEvent): void {
  if (isRunning.value) return;
  if (event.target === event.currentTarget && pointerDownOnOverlay.value) {
    emit("close");
  }
  pointerDownOnOverlay.value = false;
}

const isRunning = computed(
  () => phase.value === "implementing" || phase.value === "planning",
);

/** Status icon for each item. */
function statusIcon(status: string): string {
  switch (status) {
    case "pending": return "○";
    case "in_progress": return "◐";
    case "done": return "●";
    case "failed": return "✕";
    case "skipped": return "—";
    default: return "○";
  }
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="impl-drawer-overlay"
      :class="{ 'impl-drawer-overlay--non-modal': isRunning }"
      @pointerdown="onOverlayPointerDown"
      @click="onOverlayClick"
    >
      <aside
        class="impl-drawer"
        role="dialog"
        :aria-modal="!isRunning"
        :aria-label="t('chat.implementation.title')"
        data-testid="impl-drawer"
      >
        <!-- Header -->
        <div class="impl-drawer-head">
          <div class="impl-drawer-head-left">
            <span class="impl-drawer-title">
              {{ t("chat.implementation.title") }}
            </span>
            <span
              v-if="phase !== 'none'"
              class="impl-drawer-phase"
              :class="`impl-phase--${phase}`"
            >
              {{ t(`chat.implementation.phaseLabel.${phase}`) }}
            </span>
          </div>
          <div class="impl-drawer-head-right">
            <span v-if="items.length > 0" class="impl-drawer-progress">
              {{ doneCount }}/{{ items.length }}
            </span>
            <button
              type="button"
              class="impl-drawer-close"
              :title="t('renameDialog.cancel')"
              @click="emit('close')"
            >
              ✕
            </button>
          </div>
        </div>

        <!-- Body -->
        <div class="impl-drawer-body">
          <!-- Generate Plan CTA (phase=none) -->
          <div v-if="phase === 'none'" class="impl-generate-section">
            <p class="impl-generate-hint">
              {{ t("chat.implementation.generateHint") }}
            </p>
            <button
              type="button"
              class="impl-btn impl-btn--primary"
              data-testid="impl-generate-btn"
              :disabled="generating || !canGenerate"
              @click="onGenerate()"
            >
              {{ t("chat.implementation.controls.generate") }}
            </button>
            <p v-if="!canGenerate" class="impl-generate-warn">
              {{ t("chat.implementation.planningNeedRoster") }}
            </p>
          </div>

          <!-- Planning failed — explain + retry -->
          <div v-else-if="phase === 'planning_failed'" class="impl-generate-section">
            <p class="impl-failed-hint">
              {{ t("chat.implementation.planningFailedHint") }}
            </p>
            <p class="impl-failed-suggestion">
              {{ t("chat.implementation.planningFailedSuggestion") }}
            </p>
            <button
              type="button"
              class="impl-btn impl-btn--primary"
              :disabled="generating || !canGenerate"
              @click="onGenerate()"
            >
              {{ t("chat.implementation.controls.retry") }}
            </button>
          </div>

          <!-- Controls bar -->
          <div
            v-if="phase !== 'none' && phase !== 'planning' && phase !== 'planning_failed'"
            class="impl-controls"
          >
            <button
              v-if="phase === 'planned' || phase === 'completed'"
              type="button"
              class="impl-btn impl-btn--primary"
              :disabled="controlBusy"
              @click="onStart()"
            >
              {{ t("chat.implementation.controls.start") }}
            </button>
            <button
              v-if="phase === 'implementing'"
              type="button"
              class="impl-btn"
              :disabled="controlBusy"
              @click="onPause()"
            >
              {{ t("chat.implementation.controls.pause") }}
            </button>
            <button
              v-if="phase === 'paused'"
              type="button"
              class="impl-btn impl-btn--primary"
              :disabled="controlBusy"
              @click="onResume()"
            >
              {{ t("chat.implementation.controls.resume") }}
            </button>
            <button
              v-if="phase === 'implementing' || phase === 'paused'"
              type="button"
              class="impl-btn impl-btn--danger"
              :disabled="controlBusy"
              @click="onStop()"
            >
              {{ t("chat.implementation.controls.stop") }}
            </button>
            <button
              v-if="phase === 'failed' && failedCount > 0"
              type="button"
              class="impl-btn"
              :disabled="controlBusy"
              @click="onRetry()"
            >
              {{ t("chat.implementation.controls.retry") }}
            </button>
          </div>

          <!-- Plan document affordance — detail lives in the workspace doc,
               not in the lightweight items. -->
          <div v-if="items.length > 0" class="impl-plandoc" data-testid="impl-plandoc">
            <span class="impl-plandoc-hint">{{ docDetailHint }}</span>
            <button
              type="button"
              class="impl-plandoc-open"
              data-testid="impl-open-plandoc"
              :title="openPlanDocLabel"
            >
              📄 {{ planDocName }}
            </button>
          </div>

          <!-- Item list -->
          <ul v-if="items.length > 0" class="impl-items">
            <li
              v-for="item in items"
              :key="item.id"
              class="impl-item"
              :class="[
                `impl-item--${item.status}`,
                { 'impl-item--current': item.id === currentItem },
              ]"
            >
              <div class="impl-item-row" @click="toggleDetails(item.id)">
                <span
                  class="impl-item-status"
                  :title="t(`chat.implementation.status.${item.status}`)"
                >
                  {{ statusIcon(item.status) }}
                </span>
                <span class="impl-item-title">{{ item.title }}</span>
                <span
                  v-if="item.assignedRole"
                  class="impl-item-role"
                  data-testid="impl-item-role"
                  :title="t('chat.implementation.assignedRoleLabel')"
                >
                  {{ item.assignedRole }}
                </span>
                <span class="impl-item-chevron">
                  {{ expandedItemId === item.id ? "▴" : "▾" }}
                </span>
              </div>

              <!-- Expanded details -->
              <div v-if="expandedItemId === item.id" class="impl-item-details">
                <!-- Role assignment -->
                <div class="impl-item-field">
                  <label class="impl-item-field-label">
                    {{ t("chat.implementation.item.assignRole") }}
                  </label>
                  <select
                    class="impl-item-select"
                    :value="item.assignedRole ?? ''"
                    :disabled="item.status === 'in_progress' || item.status === 'done'"
                    @change="onRoleChange(item, ($event.target as HTMLSelectElement).value)"
                  >
                    <option
                      v-for="opt in roleOptions"
                      :key="opt.value"
                      :value="opt.value"
                    >
                      {{ opt.label }}
                    </option>
                  </select>
                </div>

                <!-- Description -->
                <p v-if="item.description" class="impl-item-desc">
                  {{ item.description }}
                </p>

                <!-- Lightweight item: detail lives in the plan doc. -->
                <p
                  v-else
                  class="impl-item-dochint"
                  data-testid="impl-item-dochint"
                >
                  {{ docDetailHint }}
                </p>

                <!-- Acceptance criteria -->
                <div v-if="item.acceptanceCriteria.length > 0" class="impl-item-field">
                  <span class="impl-item-field-label">
                    {{ t("chat.implementation.item.acceptanceCriteria") }}
                  </span>
                  <ul class="impl-item-criteria">
                    <li v-for="(c, i) in item.acceptanceCriteria" :key="i">
                      {{ c }}
                    </li>
                  </ul>
                </div>

                <!-- Result / Error -->
                <p v-if="item.resultSummary" class="impl-item-result">
                  <strong>{{ t("chat.implementation.item.resultSummary") }}:</strong>
                  {{ item.resultSummary }}
                </p>
                <p v-if="item.lastError" class="impl-item-error">
                  <strong>{{ t("chat.implementation.item.lastError") }}:</strong>
                  {{ item.lastError }}
                </p>

                <!-- Actions -->
                <div class="impl-item-actions">
                  <button
                    v-if="item.status === 'pending'"
                    type="button"
                    class="impl-item-action"
                    :title="t('chat.implementation.item.skip')"
                    @click="onSkip(item)"
                  >
                    {{ t("chat.implementation.item.skip") }}
                  </button>
                  <button
                    v-if="item.status !== 'in_progress'"
                    type="button"
                    class="impl-item-action impl-item-action--danger"
                    :title="t('chat.implementation.item.delete')"
                    @click="onDelete(item)"
                  >
                    {{ t("chat.implementation.item.delete") }}
                  </button>
                </div>
              </div>
            </li>
          </ul>

          <!-- Add item -->
          <div
            v-if="phase === 'planned' || phase === 'paused' || phase === 'failed'"
            class="impl-add-item"
          >
            <input
              v-model="newItemTitle"
              type="text"
              class="impl-add-input"
              :placeholder="t('chat.implementation.item.addTitle')"
              @keydown.enter="onAddItem()"
            />
            <button
              type="button"
              class="impl-btn impl-btn--small"
              :disabled="!newItemTitle.trim()"
              @click="onAddItem()"
            >
              {{ t("chat.implementation.item.add") }}
            </button>
          </div>

          <!-- Error -->
          <p v-if="impl.error.value" class="impl-error" data-testid="impl-error">
            {{ impl.error.value }}
          </p>
        </div>
      </aside>
    </div>
  </Teleport>
</template>

<style scoped>
/* -- Overlay -- */
.impl-drawer-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(0, 0, 0, 0.28);
  display: flex;
  justify-content: flex-end;
  align-items: flex-start;
  padding: 12px;
}
.impl-drawer-overlay--non-modal {
  background: transparent;
  pointer-events: none;
}
.impl-drawer-overlay--non-modal .impl-drawer {
  pointer-events: auto;
}

/* -- Drawer panel -- */
.impl-drawer {
  width: min(400px, 92vw);
  height: auto;
  max-height: calc(100vh - 24px);
  background: var(--bg-secondary, #1e1e1e);
  border: 1px solid var(--border);
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.35);
}

/* -- Header -- */
.impl-drawer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
  gap: 8px;
}
.impl-drawer-head-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.impl-drawer-head-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.impl-drawer-title {
  font-size: var(--text-base, 13px);
  font-weight: 600;
  color: var(--text-primary, #eee);
}
.impl-drawer-phase {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: var(--radius-sm, 4px);
  background: var(--bg-tertiary, #2a2a2a);
  color: var(--text-secondary, #aaa);
}
.impl-phase--implementing {
  color: var(--accent, #5b9bd5);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}
.impl-phase--completed {
  color: var(--success, #4caf50);
  background: color-mix(in srgb, var(--success) 12%, transparent);
}
.impl-phase--failed,
.impl-phase--planning_failed {
  color: var(--error, #e57373);
  background: color-mix(in srgb, var(--error) 12%, transparent);
}
.impl-phase--paused {
  color: var(--text-muted, #666);
}
.impl-drawer-progress {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary, #aaa);
  font-variant-numeric: tabular-nums;
}
.impl-drawer-close {
  background: none;
  border: none;
  color: var(--text-secondary, #aaa);
  cursor: pointer;
  font-size: 14px;
  padding: 2px 4px;
}
.impl-drawer-close:hover {
  color: var(--text-primary, #eee);
}

/* -- Body -- */
.impl-drawer-body {
  flex: 0 1 auto;
  overflow-y: auto;
  min-height: 0;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* -- Generate CTA -- */
.impl-generate-section {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 16px 0;
  text-align: center;
}
.impl-generate-hint {
  margin: 0;
  font-size: 12px;
  color: var(--text-secondary, #aaa);
  line-height: 1.5;
}
.impl-generate-warn {
  margin: 0;
  font-size: 11px;
  color: var(--text-muted, #666);
  font-style: italic;
}
.impl-failed-hint {
  margin: 0;
  font-size: 12px;
  color: var(--error, #e57373);
  line-height: 1.5;
}
.impl-failed-suggestion {
  margin: 0;
  font-size: 11px;
  color: var(--text-secondary, #aaa);
  line-height: 1.5;
}

/* -- Buttons -- */
.impl-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg-tertiary, #2a2a2a);
  color: var(--text-primary, #eee);
  font-size: 12px;
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
}
.impl-btn:hover:not(:disabled) {
  background: var(--bg-hover, #333);
  border-color: var(--text-muted);
}
.impl-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.impl-btn--primary {
  background: var(--accent, #5b9bd5);
  border-color: var(--accent, #5b9bd5);
  color: #fff;
}
.impl-btn--primary:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 85%, #000);
  border-color: color-mix(in srgb, var(--accent) 85%, #000);
}
.impl-btn--danger {
  color: var(--error, #e57373);
  border-color: var(--error, #e57373);
}
.impl-btn--danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--error) 12%, transparent);
}
.impl-btn--small {
  padding: 4px 8px;
  font-size: 11px;
}

/* -- Controls bar -- */
.impl-controls {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

/* -- Items list -- */
.impl-items {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.impl-item {
  border-radius: var(--radius-sm, 4px);
  border: 1px solid transparent;
  transition: border-color 0.12s;
}
.impl-item--current {
  border-color: var(--accent, #5b9bd5);
  background: color-mix(in srgb, var(--accent) 5%, transparent);
}

/* -- Item row (summary line) -- */
.impl-item-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  cursor: pointer;
  border-radius: var(--radius-sm, 4px);
  transition: background 0.1s;
}
.impl-item-row:hover {
  background: var(--bg-hover, #2a2a2a);
}
.impl-item-status {
  flex-shrink: 0;
  width: 16px;
  text-align: center;
  font-size: 12px;
}
.impl-item--pending .impl-item-status { color: var(--text-muted, #666); }
.impl-item--in_progress .impl-item-status { color: var(--accent, #5b9bd5); }
.impl-item--done .impl-item-status { color: var(--success, #4caf50); }
.impl-item--failed .impl-item-status { color: var(--error, #e57373); }
.impl-item--skipped .impl-item-status { color: var(--text-muted, #666); }

.impl-item-title {
  flex: 1;
  font-size: 12px;
  color: var(--text-primary, #eee);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.impl-item--done .impl-item-title {
  text-decoration: line-through;
  opacity: 0.7;
}
.impl-item--skipped .impl-item-title {
  text-decoration: line-through;
  opacity: 0.5;
}
.impl-item-chevron {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--text-muted, #666);
}

/* -- Item details (expanded) -- */
.impl-item-details {
  padding: 6px 8px 10px 32px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.impl-item-field {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.impl-item-field-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-muted, #666);
  font-weight: 600;
}
.impl-item-select {
  font-size: 12px;
  padding: 3px 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg-input, #1a1a1a);
  color: var(--text-primary, #eee);
}
.impl-item-select:disabled {
  opacity: 0.5;
}
.impl-item-desc {
  margin: 0;
  font-size: 11px;
  color: var(--text-secondary, #aaa);
  line-height: 1.4;
}
.impl-item-dochint {
  margin: 0;
  font-size: 11px;
  font-style: italic;
  color: var(--text-secondary, #aaa);
  line-height: 1.4;
}
.impl-item-role {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--surface-2, rgba(255, 255, 255, 0.08));
  color: var(--text-secondary, #aaa);
  white-space: nowrap;
}
.impl-plandoc {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 6px 8px;
  margin-bottom: 8px;
  border-radius: 6px;
  background: var(--surface-2, rgba(255, 255, 255, 0.05));
}
.impl-plandoc-hint {
  flex: 1;
  font-size: 11px;
  color: var(--text-secondary, #aaa);
}
.impl-plandoc-open {
  font-size: 11px;
  padding: 2px 8px;
  border: 1px solid var(--border, rgba(255, 255, 255, 0.15));
  border-radius: 6px;
  background: transparent;
  color: var(--text-primary, #eee);
  cursor: pointer;
  white-space: nowrap;
}
.impl-plandoc-open:hover {
  background: var(--surface-3, rgba(255, 255, 255, 0.1));
}
.impl-item-criteria {
  margin: 0;
  padding-left: 16px;
  font-size: 11px;
  color: var(--text-secondary, #aaa);
  line-height: 1.5;
}
.impl-item-result {
  margin: 0;
  font-size: 11px;
  color: var(--success, #4caf50);
}
.impl-item-error {
  margin: 0;
  font-size: 11px;
  color: var(--error, #e57373);
}
.impl-item-actions {
  display: flex;
  gap: 6px;
}
.impl-item-action {
  font-size: 11px;
  padding: 2px 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: none;
  color: var(--text-secondary, #aaa);
  cursor: pointer;
}
.impl-item-action:hover {
  background: var(--bg-hover, #333);
  color: var(--text-primary, #eee);
}
.impl-item-action--danger {
  color: var(--error, #e57373);
  border-color: color-mix(in srgb, var(--error) 40%, transparent);
}
.impl-item-action--danger:hover {
  background: color-mix(in srgb, var(--error) 12%, transparent);
}

/* -- Add item -- */
.impl-add-item {
  display: flex;
  gap: 6px;
  align-items: center;
}
.impl-add-input {
  flex: 1;
  font-size: 12px;
  padding: 5px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg-input, #1a1a1a);
  color: var(--text-primary, #eee);
}
.impl-add-input::placeholder {
  color: var(--text-muted, #666);
}

/* -- Error -- */
.impl-error {
  margin: 0;
  font-size: 11px;
  color: var(--error, #e57373);
  padding: 6px 8px;
  background: color-mix(in srgb, var(--error) 8%, transparent);
  border-radius: var(--radius-sm, 4px);
}
</style>
