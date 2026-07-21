<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  ImplementationPanel.vue — implementation-run observability + control panel
  (DISC-1 二期 §22.9).

  A self-contained panel (rendered in ChatView between the message list and the
  composer) that surfaces the OFF-by-default implementation orchestration:

    • shows the live plan (run phase + done/total progress),
    • shows each feature item with its status, assigned role, result / error,
    • lets the user EDIT items before/between runs — re-assign role, rename,
      skip (pending↔skipped), delete, add — persisted via the merge-by-id PATCH,
    • drives execution control (start / pause / resume / stop) by SENDING a
      localized control message through the ordinary chat send path (control
      router), NOT a dedicated route.

  The panel renders NOTHING when `phase === "none"` (no plan) — ordinary chat /
  discussion is therefore visually unchanged until an implementation run starts.

  All confirmation prompts use `useConfirm()` (定制对话框) — NEVER
  window.confirm/alert/prompt (AGENTS.md §3.9.2). Colours / spacing are theme
  tokens (`var(--…)`), never hardcoded literals.

  Cohesion: the data / CRUD / control logic lives in `useImplementation` + the
  implementation store; this component is presentation + local draft-form state
  only, well under the 1000-line软上限.
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useConfirm } from "@/composables/useConfirm";
import { useImplementation } from "@/composables/chat/useImplementation";
import { useDiscussion } from "@/composables/chat/useDiscussion";
import { useChatTabsStore } from "@/stores/chatTabs";
import type { ImplementationItemVM } from "@/stores/_chatTabsTypes";

const { t } = useI18n();
const { confirm } = useConfirm();
const impl = useImplementation();
const discussion = useDiscussion();

const phase = impl.phase;
const items = impl.items;
const currentItem = impl.currentItem;

/** The roster of named roles (reused from the discussion config) — drives the
 *  assigned-role dropdown so the user re-assigns an item to an existing role
 *  rather than free-typing. */
const roster = computed(() => discussion.participants.value);

/** Role names offered in the assign dropdown (display names, de-duplicated). */
const roleOptions = computed<string[]>(() => {
  const names = roster.value.map((p) => p.display_name).filter((n) => n !== "");
  return Array.from(new Set(names));
});

// ── Progress summary (done / total) ─────────────────────────────────────────
const totalCount = computed(() => items.value.length);
const doneCount = computed(
  () => items.value.filter((it) => it.status === "done").length,
);

// ── DISC-1 三期-step6 — at-a-glance status overview ─────────────────────────
// A compact one-row breakdown (counts per status) so the whole run's progress
// is visible without scanning every row. Only non-zero buckets render, keeping
// the bar tight (avoids element overload — 用户 2026-06-24 确认 方案1).
const STATUS_ORDER = [
  "in_progress",
  "pending",
  "done",
  "failed",
  "skipped",
] as const;
const statusCounts = computed<{ status: string; count: number }[]>(() => {
  const tally: Record<string, number> = {};
  for (const it of items.value) tally[it.status] = (tally[it.status] ?? 0) + 1;
  return STATUS_ORDER.filter((s) => (tally[s] ?? 0) > 0).map((s) => ({
    status: s,
    count: tally[s] ?? 0,
  }));
});
const donePercent = computed(() =>
  totalCount.value === 0
    ? 0
    : Math.round((doneCount.value / totalCount.value) * 100),
);

// ── DISC-1 三期-step6 — per-item detail fold (which rows are expanded) ───────
// Detail (description / acceptance / verify command / depends-on) is COLLAPSED
// by default so the list stays as compact as before; the user expands a row to
// see / edit the extra fields (avoids element overload — 用户 2026-06-24).
const expanded = reactive<Record<string, boolean>>({});
function toggleExpand(id: string): void {
  expanded[id] = !expanded[id];
}
function isExpanded(id: string): boolean {
  return expanded[id] === true;
}

/** Local draft of each item's verify_command while the row is expanded, so the
 *  input is responsive and we only PATCH on blur/Enter (not every keystroke). */
const verifyDraft = reactive<Record<string, string>>({});
function verifyValue(it: ImplementationItemVM): string {
  return verifyDraft[it.id] ?? it.verifyCommand;
}
function onVerifyInput(it: ImplementationItemVM, e: Event): void {
  verifyDraft[it.id] = (e.target as HTMLInputElement).value;
}
function commitVerify(it: ImplementationItemVM): void {
  const next = (verifyDraft[it.id] ?? it.verifyCommand).trim();
  if (next === it.verifyCommand) return;
  void impl.updateItem(it.id, { verifyCommand: next });
}


// ── Control-button visibility (derived from the run phase) ──────────────────
const canStart = computed(() => phase.value === "planned");
const canPauseStop = computed(() => phase.value === "implementing");
const canResume = computed(() => phase.value === "paused");
// 三期-step2: retry is offered when the run ended/parked with at least one
// FAILED item to re-run (resetting it to pending + re-running on the backend).
const canRetry = computed(
  () =>
    (phase.value === "failed" || phase.value === "paused") &&
    items.value.some((it) => it.status === "failed"),
);

// ── Add-item draft form ─────────────────────────────────────────────────────
const showAdd = ref(false);
const draft = reactive<{ title: string; assignedRole: string }>({
  title: "",
  assignedRole: "",
});

const canAdd = computed(() => draft.title.trim() !== "");

function openAdd(): void {
  draft.title = "";
  draft.assignedRole = "";
  showAdd.value = true;
}

function cancelAdd(): void {
  showAdd.value = false;
}

async function submitAdd(): Promise<void> {
  if (!canAdd.value) return;
  await impl.addItem({
    title: draft.title.trim(),
    ...(draft.assignedRole !== "" ? { assignedRole: draft.assignedRole } : {}),
  });
  if (impl.error.value === null) {
    showAdd.value = false;
    draft.title = "";
    draft.assignedRole = "";
  }
}

/** Whether an item is the one currently being implemented (highlight + lock its
 *  role select while it runs — backend rejects editing the current item with a
 *  409). */
function isCurrent(it: ImplementationItemVM): boolean {
  return currentItem.value !== null && it.id === currentItem.value;
}

/** A pending item is the only kind the user may skip. */
function canSkip(it: ImplementationItemVM): boolean {
  return it.status === "pending";
}

function onAssignRole(it: ImplementationItemVM, e: Event): void {
  const value = (e.target as HTMLSelectElement).value;
  void impl.updateItem(it.id, { assignedRole: value === "" ? null : value });
}

async function confirmDelete(it: ImplementationItemVM): Promise<void> {
  // §3.9.2: custom confirm dialog — NEVER window.confirm.
  const ok = await confirm({
    icon: "🗑️",
    title: t("chat.implementation.confirmDelete.title"),
    message: t("chat.implementation.confirmDelete.message", { title: it.title }),
    confirmText: t("common.delete"),
    cancelText: t("common.cancel"),
    confirmStyle: "danger",
  });
  if (ok) await impl.deleteItem(it.id);
}

function onSkip(it: ImplementationItemVM): void {
  void impl.skipItem(it.id);
}

// ── Refresh the plan on mount + when the active conversation changes ─────────
// A reload rebuilds the plan after a page refresh / tab switch (the SSE frames
// only fill an in-flight run); when `phase === "none"` the panel stays hidden,
// so this is a cheap idempotent refresh that never affects ordinary chat.
const tabs = useChatTabsStore();
const activeConversationId = computed(
  () => tabs.activeTab?.conversationId ?? null,
);

onMounted(() => {
  void impl.reload();
});

watch(activeConversationId, () => {
  void impl.reload();
});
</script>

<template>
  <div
    v-if="phase !== 'none'"
    class="impl-panel"
    data-testid="implementation-panel"
  >
    <!-- ── Header: phase label + progress ── -->
    <div class="impl-header">
      <span class="impl-phase" :data-testid="`impl-phase-${phase}`">
        {{ t(`chat.implementation.phaseLabel.${phase}`) }}
      </span>
      <span class="impl-progress" data-testid="impl-progress">
        {{ doneCount }} / {{ totalCount }}
      </span>
    </div>

    <!-- ── DISC-1 三期-step6: at-a-glance progress overview ── -->
    <div
      v-if="totalCount > 0"
      class="impl-overview"
      data-testid="impl-overview"
    >
      <div class="impl-overview-bar" :title="`${donePercent}%`">
        <div
          class="impl-overview-fill"
          :style="{ width: donePercent + '%' }"
        ></div>
      </div>
      <div class="impl-overview-counts">
        <span
          v-for="b in statusCounts"
          :key="b.status"
          class="impl-overview-chip"
          :class="`impl-overview-chip--${b.status}`"
          :data-testid="`impl-overview-${b.status}`"
        >
          {{ t(`chat.implementation.status.${b.status}`) }} {{ b.count }}
        </span>
      </div>
    </div>

    <!-- ── Controls ── -->
    <div class="impl-controls">
      <button
        v-if="canStart"
        type="button"
        class="impl-btn impl-btn--primary"
        data-testid="impl-start-btn"
        @click="impl.start()"
      >
        ▶ {{ t("chat.implementation.controls.start") }}
      </button>
      <button
        v-if="canResume"
        type="button"
        class="impl-btn impl-btn--primary"
        data-testid="impl-resume-btn"
        @click="impl.resume()"
      >
        ▶ {{ t("chat.implementation.controls.resume") }}
      </button>
      <button
        v-if="canPauseStop"
        type="button"
        class="impl-btn"
        data-testid="impl-pause-btn"
        @click="impl.pause()"
      >
        ⏸ {{ t("chat.implementation.controls.pause") }}
      </button>
      <button
        v-if="canPauseStop"
        type="button"
        class="impl-btn impl-btn--danger"
        data-testid="impl-stop-btn"
        @click="impl.stop()"
      >
        ⏹ {{ t("chat.implementation.controls.stop") }}
      </button>
      <button
        v-if="canRetry"
        type="button"
        class="impl-btn impl-btn--primary"
        data-testid="impl-retry-btn"
        @click="impl.retry()"
      >
        ↻ {{ t("chat.implementation.controls.retry") }}
      </button>
    </div>

    <!-- ── Item list ── -->
    <ul class="impl-list" data-testid="impl-list">
      <li
        v-for="(it, idx) in items"
        :key="it.id"
        class="impl-item"
        :class="{ 'is-current': isCurrent(it) }"
        :data-testid="`impl-item-${it.id}`"
      >
        <span class="impl-item-index" aria-hidden="true">{{ idx + 1 }}</span>
        <div class="impl-item-body">
          <div class="impl-item-row">
            <span class="impl-item-title">{{ it.title }}</span>
            <span
              class="impl-item-status"
              :class="`impl-item-status--${it.status}`"
              >{{ t(`chat.implementation.status.${it.status}`) }}</span
            >
            <button
              type="button"
              class="impl-icon-btn impl-item-expand"
              :aria-expanded="isExpanded(it.id)"
              :title="t('chat.implementation.item.details')"
              :data-testid="`impl-item-expand-${it.id}`"
              @click="toggleExpand(it.id)"
            >
              {{ isExpanded(it.id) ? "▾" : "▸" }}
            </button>
          </div>
          <div class="impl-item-row impl-item-row--meta">
            <label class="impl-item-role">
              <span class="impl-item-role-label">{{
                t("chat.implementation.item.assignRole")
              }}</span>
              <select
                class="impl-item-role-select"
                data-testid="impl-item-role-select"
                :value="it.assignedRole ?? ''"
                :disabled="isCurrent(it)"
                @change="onAssignRole(it, $event)"
              >
                <option value="">
                  {{ t("chat.implementation.item.roleUnassigned") }}
                </option>
                <option
                  v-if="it.assignedRole && !roleOptions.includes(it.assignedRole)"
                  :value="it.assignedRole"
                >
                  {{ it.assignedRole }}
                </option>
                <option v-for="r in roleOptions" :key="r" :value="r">
                  {{ r }}
                </option>
              </select>
            </label>
            <div class="impl-item-actions">
              <button
                v-if="canSkip(it)"
                type="button"
                class="impl-icon-btn"
                :title="t('chat.implementation.item.skip')"
                data-testid="impl-item-skip"
                @click="onSkip(it)"
              >
                ⏭
              </button>
              <button
                type="button"
                class="impl-icon-btn impl-icon-btn--danger"
                :title="t('chat.implementation.item.delete')"
                data-testid="impl-item-delete"
                @click="confirmDelete(it)"
              >
                🗑️
              </button>
            </div>
          </div>
          <p
            v-if="it.resultSummary"
            class="impl-item-result"
            data-testid="impl-item-result"
          >
            {{ t("chat.implementation.item.resultSummary") }}: {{ it.resultSummary }}
          </p>
          <p
            v-if="it.lastError"
            class="impl-item-error"
            data-testid="impl-item-error"
          >
            {{ t("chat.implementation.item.lastError") }}: {{ it.lastError }}
          </p>

          <!-- ── DISC-1 三期-step6: collapsible item detail ── -->
          <div
            v-if="isExpanded(it.id)"
            class="impl-item-detail"
            :data-testid="`impl-item-detail-${it.id}`"
          >
            <p v-if="it.description" class="impl-detail-row">
              <span class="impl-detail-label">{{
                t("chat.implementation.item.description")
              }}</span>
              <span class="impl-detail-text">{{ it.description }}</span>
            </p>
            <div
              v-if="it.acceptanceCriteria.length > 0"
              class="impl-detail-row"
            >
              <span class="impl-detail-label">{{
                t("chat.implementation.item.acceptanceCriteria")
              }}</span>
              <ul class="impl-detail-criteria">
                <li v-for="(c, ci) in it.acceptanceCriteria" :key="ci">
                  {{ c }}
                </li>
              </ul>
            </div>
            <p
              v-if="it.dependsOn.length > 0"
              class="impl-detail-row"
            >
              <span class="impl-detail-label">{{
                t("chat.implementation.item.dependsOn")
              }}</span>
              <span class="impl-detail-text">{{ it.dependsOn.join(", ") }}</span>
            </p>
            <label class="impl-detail-row impl-detail-verify">
              <span class="impl-detail-label">{{
                t("chat.implementation.item.verifyCommand")
              }}</span>
              <input
                type="text"
                class="impl-verify-input"
                :placeholder="t('chat.implementation.item.verifyCommandPlaceholder')"
                :value="verifyValue(it)"
                :disabled="isCurrent(it)"
                :data-testid="`impl-item-verify-${it.id}`"
                @input="onVerifyInput(it, $event)"
                @blur="commitVerify(it)"
                @keyup.enter="commitVerify(it)"
              />
            </label>
            <p class="impl-detail-hint">
              {{ t("chat.implementation.item.verifyCommandHint") }}
            </p>
            <p
              v-if="it.attemptCount > 0"
              class="impl-detail-row impl-detail-attempts"
            >
              <span class="impl-detail-label">{{
                t("chat.implementation.item.attempts")
              }}</span>
              <span class="impl-detail-text">{{ it.attemptCount }}</span>
            </p>
          </div>
        </div>
      </li>
    </ul>

    <!-- ── Add item ── -->
    <div class="impl-add">
      <button
        v-if="!showAdd"
        type="button"
        class="impl-btn impl-btn--ghost"
        data-testid="impl-add-item"
        @click="openAdd"
      >
        + {{ t("chat.implementation.item.add") }}
      </button>
      <div v-else class="impl-add-form" data-testid="impl-add-form">
        <input
          v-model="draft.title"
          type="text"
          class="impl-add-input"
          data-testid="impl-add-title"
          :placeholder="t('chat.implementation.item.addTitle')"
        />
        <select
          v-model="draft.assignedRole"
          class="impl-item-role-select"
          data-testid="impl-add-role"
        >
          <option value="">
            {{ t("chat.implementation.item.roleUnassigned") }}
          </option>
          <option v-for="r in roleOptions" :key="r" :value="r">{{ r }}</option>
        </select>
        <button
          type="button"
          class="impl-btn"
          data-testid="impl-add-cancel"
          @click="cancelAdd"
        >
          {{ t("common.cancel") }}
        </button>
        <button
          type="button"
          class="impl-btn impl-btn--primary"
          :disabled="!canAdd"
          data-testid="impl-add-submit"
          @click="submitAdd"
        >
          {{ t("common.add") }}
        </button>
      </div>
    </div>

    <p v-if="impl.error.value" class="impl-error" data-testid="impl-error">
      {{ impl.error.value }}
    </p>
  </div>
</template>

<style scoped>
.impl-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: var(--space-2) var(--space-3) 0;
  padding: var(--space-3);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text-primary);
  box-sizing: border-box;
  max-height: 40vh;
  overflow-y: auto;
}
.impl-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.impl-phase {
  font-weight: var(--weight-semibold);
}
.impl-progress {
  font-size: var(--text-sm);
  color: var(--text-muted);
}
.impl-controls {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.impl-btn {
  padding: var(--space-1) var(--space-3);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  cursor: pointer;
  font-size: var(--text-sm);
}
.impl-btn:hover {
  background: var(--bg-hover);
}
.impl-btn--primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.impl-btn--danger {
  border-color: var(--error);
  color: var(--error);
}
.impl-btn--ghost {
  background: transparent;
  color: var(--text-secondary);
}
.impl-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.impl-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.impl-item {
  display: flex;
  gap: var(--space-2);
  padding: var(--space-2);
  background: var(--bg-tertiary);
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
}
.impl-item.is-current {
  border-color: var(--accent);
  background: var(--accent-muted);
}
.impl-item-index {
  flex-shrink: 0;
  width: 20px;
  text-align: right;
  color: var(--text-muted);
  font-size: var(--text-sm);
}
.impl-item-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.impl-item-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.impl-item-row--meta {
  gap: var(--space-2);
}
.impl-item-title {
  font-weight: var(--weight-medium);
  overflow: hidden;
  text-overflow: ellipsis;
}
.impl-item-status {
  flex-shrink: 0;
  font-size: var(--text-xs);
  padding: 2px var(--space-2);
  border-radius: var(--radius-full);
  background: var(--bg-input);
  color: var(--text-secondary);
}
.impl-item-status--in_progress {
  background: var(--accent-muted);
  color: var(--accent);
}
.impl-item-status--done {
  background: var(--banner-success-bg, var(--accent-muted));
  color: var(--success, var(--accent));
}
.impl-item-status--failed {
  background: var(--banner-error-bg);
  color: var(--error);
}
.impl-item-status--skipped {
  color: var(--text-muted);
}
.impl-item-role {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  min-width: 0;
  flex: 1;
}
.impl-item-role-label {
  font-size: var(--text-xs);
  color: var(--text-muted);
  white-space: nowrap;
}
.impl-item-role-select {
  flex: 1 1 auto;
  min-width: 0;
  padding: var(--space-1) var(--space-2);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font: inherit;
}
.impl-item-role-select:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.impl-item-actions {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
}
.impl-icon-btn {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.impl-icon-btn:hover {
  background: var(--bg-hover);
}
.impl-icon-btn--danger:hover {
  background: var(--banner-error-bg);
}
.impl-item-result {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--text-muted);
}
.impl-item-error {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--error);
}
.impl-add {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.impl-add-form {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}
.impl-add-input {
  flex: 1 1 160px;
  min-width: 0;
  padding: var(--space-1) var(--space-2);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font: inherit;
}
.impl-error {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--error);
}

/* ── DISC-1 三期-step6: progress overview ── */
.impl-overview {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.impl-overview-bar {
  height: 6px;
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  overflow: hidden;
}
.impl-overview-fill {
  height: 100%;
  background: var(--success, var(--accent));
  transition: width 0.3s ease;
}
.impl-overview-counts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}
.impl-overview-chip {
  font-size: var(--text-xs);
  padding: 0 var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text-secondary);
}
.impl-overview-chip--in_progress {
  color: var(--accent);
}
.impl-overview-chip--done {
  color: var(--success, var(--accent));
}
.impl-overview-chip--failed {
  color: var(--error);
}

/* ── DISC-1 三期-step6: per-item detail fold ── */
.impl-item-expand {
  margin-left: auto;
}
.impl-item-detail {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: var(--space-1);
  padding-top: var(--space-1);
  border-top: 1px dashed var(--border);
}
.impl-detail-row {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--text-secondary);
}
.impl-detail-label {
  font-weight: 600;
  margin-right: var(--space-1);
  color: var(--text-muted);
}
.impl-detail-criteria {
  margin: var(--space-1) 0 0;
  padding-left: var(--space-4);
}
.impl-detail-verify {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.impl-verify-input {
  flex: 1 1 160px;
  min-width: 0;
  padding: var(--space-1) var(--space-2);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font: inherit;
  font-size: var(--text-xs);
}
.impl-verify-input:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.impl-detail-hint {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--text-muted);
}
</style>
