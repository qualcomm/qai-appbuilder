<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * BudgetPopover — dedicated per-conversation TOKEN budget (`max_budget_tokens`)
 * configuration popover (extracted from `SessionToolsPopover` so the budget has
 * its OWN toolbar button instead of being buried in the tools/skills panel).
 *
 * Surfaced from a dedicated wallet button in the composer's `.rit-right` row.
 * The cap + running counter are PERSISTED in `Conversation.meta.budget`
 * ({ max_tokens, used_tokens }) — unlike the in-memory per-session tool
 * override. This popover:
 *   - reads the snapshot on open / session switch (`refreshBudget`);
 *   - PATCHes `.../budget` on blur / Enter (`commitBudget`);
 *   - resets the running counter (`onResetUsed`);
 *   - emits `saved` after a successful write so the composer's OWN budget badge
 *     instance refreshes immediately (the badge would otherwise only update on
 *     a session switch or a budget-exceeded signal).
 *
 * Scope (product spec): the budget covers the WHOLE conversation tree — the
 * main agent AND all its sub-/grand-sub-agents observe against the SAME
 * `root_conversation_id` budget pool (see `agent_tool.py` /
 * `budget_tracker.py`). Only the MAIN conversation exposes this control; a
 * sub-agent tab hides the trigger (enforced by the composer's `v-if`), so a
 * user sets ONE cap for the whole session.
 */
import { computed, onMounted, onBeforeUnmount, ref, useTemplateRef, watch } from "vue";
import { useI18n } from "vue-i18n";

import { useChatTabsStore } from "@/stores/chatTabs";
import { useConversationBudget } from "@/composables/chat/useConversationBudget";

interface Props {
  open: boolean;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  "update:open": [value: boolean];
  /** Fired after a successful cap save / reset so the composer badge refreshes. */
  saved: [];
}>();

const { t } = useI18n();
const store = useChatTabsStore();

const popoverRef = useTemplateRef<HTMLDivElement>("popover");

const activeTab = computed(() => store.activeTab);

// The active tab's conversation id keys the persisted budget (null for a fresh,
// unsent tab → the section shows a disabled hint).
const budgetConvId = computed<string | null>(
  () => activeTab.value?.conversationId ?? null,
);
const {
  snapshot: budgetSnapshot,
  enabled: budgetEnabled,
  save: saveBudget,
  reset: resetBudget,
  refresh: refreshBudget,
} = useConversationBudget(budgetConvId);

// Local text model for the number input. Empty string = "no limit" (disabled).
const budgetInput = ref<string>("");

// True when the active tab has no persisted conversation yet — the budget
// cannot be set until the first turn creates the conversation.
const budgetUnavailable = computed<boolean>(() => budgetConvId.value === null);

// Sync the input from the loaded snapshot (session switch / (re)open).
watch(
  budgetSnapshot,
  (snap) => {
    budgetInput.value =
      snap !== null && snap.max_tokens !== null ? String(snap.max_tokens) : "";
  },
  { immediate: true },
);

/** Parse the input to a positive integer cap, or `null` (blank / non-positive
 *  = disable the budget). */
function parseBudgetInput(): number | null {
  const raw = String(budgetInput.value ?? "").trim();
  if (raw === "") return null;
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n;
}

/** Persist the cap on blur / Enter (PATCH .../budget). Normalises the field to
 *  the effective value the backend returns, then notifies the composer. */
async function commitBudget(): Promise<void> {
  if (budgetUnavailable.value) return;
  const cap = parseBudgetInput();
  // Snapshot the dismiss target BEFORE the PATCH. `saveBudget` is an HTTP
  // round-trip (100-500ms on slow links); during the await the user can
  // switch tabs and `activeTab.value` would then point at a DIFFERENT tab
  // than the one this commit was issued on. Dismissing on the wrong tab
  // would (a) leak the current tab's pending decision uncleared and
  // (b) — worse — silently drop another tab's pending decision if that
  // tab happens to have one. Same class of "state-vs-actual" bug that
  // caused the original 切走切回 弹窗 regression (AGENTS.md §5
  // State-Truth-First: snapshot the exact target at issue time, don't
  // re-derive it from a mutable getter after an async gap).
  const tabIdAtIssue = activeTab.value?.id;
  const pendingAtIssue = activeTab.value?.budgetDecision;
  const snap = await saveBudget(cap, false);
  // Reflect the authoritative result (State-Truth-First): normalise the field
  // to the effective cap the backend applied.
  budgetInput.value =
    snap !== null && snap.max_tokens !== null ? String(snap.max_tokens) : "";
  // Dismiss any PENDING budget-exceeded decision on the tab this commit was
  // issued on (see snapshot above) whenever the cap change makes it moot
  // (root-cause fix, 2026-07-14 — "取消 token 上限后切走切回弹窗又自动弹出"
  // user report):
  //   • cap === null  : user disabled the budget entirely → the pending
  //     decision has no cap to compare against, dismiss it.
  //   • cap > decision.max : user raised the cap manually past the point
  //     that tripped the pending decision → the trip is retroactively
  //     resolved, dismiss so it does not re-fire on tab-switch.
  // If the cap change does NOT resolve the pending decision (e.g. user
  // lowered the cap further, or raised it but not past the trip point),
  // the decision remains actionable — leave it alone.
  if (tabIdAtIssue !== undefined && pendingAtIssue !== undefined) {
    const effectiveCap = snap?.max_tokens ?? null;
    const decisionResolved =
      effectiveCap === null || effectiveCap > pendingAtIssue.max;
    if (decisionResolved) {
      store.dismissBudgetDecision(tabIdAtIssue);
    }
  }
  emit("saved");
}

/** Reset the running used-tokens counter (PATCH reset_used:true). */
async function onResetUsed(): Promise<void> {
  if (budgetUnavailable.value) return;
  // Snapshot BEFORE the async reset (see commitBudget for the rationale —
  // an in-flight PATCH lets the user switch tabs and the dismiss target
  // would drift to a different tab).
  //
  // Semantics of dismiss-on-reset: a pending budget decision carries the
  // used-token snapshot from the END frame that tripped it. After the user
  // resets used_tokens to 0, that snapshot is stale — the "you've used
  // 166499 / cap 100000, continue?" question is no longer meaningful; the
  // counter is now back at 0 and the next turn will re-evaluate from
  // scratch. Leaving the decision would (on tab-switch-back) re-open the
  // dialog with numbers that contradict the new counter, confusing users.
  // A NEW hit after the reset re-arms via handleEnd normally.
  const tabIdAtIssue = activeTab.value?.id;
  const pendingAtIssue = activeTab.value?.budgetDecision;
  await resetBudget();
  if (tabIdAtIssue !== undefined && pendingAtIssue !== undefined) {
    store.dismissBudgetDecision(tabIdAtIssue);
  }
  emit("saved");
}

function close(): void {
  emit("update:open", false);
}

// Re-read the persisted budget snapshot each time the popover opens so a budget
// consumed by turns since it was last shown is reflected.
watch(
  () => props.open,
  (next: boolean) => {
    if (next) {
      void refreshBudget();
    }
  },
);

// Click-outside to close (capture-phase mousedown; let the trigger's own
// wrapper handle the toggle — same pattern as SessionToolsPopover).
function onDocMouseDown(ev: MouseEvent): void {
  if (!props.open) return;
  const el = popoverRef.value;
  if (el === null) return;
  const target = ev.target as Node | null;
  if (target === null) return;
  if (el.contains(target)) return;
  const wrap = el.parentElement;
  if (wrap !== null && wrap.contains(target)) return;
  close();
}

onMounted(() => {
  document.addEventListener("mousedown", onDocMouseDown, true);
});

onBeforeUnmount(() => {
  document.removeEventListener("mousedown", onDocMouseDown, true);
});
</script>

<template>
  <div
    v-if="open"
    ref="popover"
    class="budget-popover"
    role="dialog"
    aria-modal="false"
    :aria-label="t('chat.budgetPopover.title', 'Token 用量预算')"
    data-testid="budget-popover"
    @mousedown.stop
    @keydown.escape="close"
  >
    <div class="budget-popover-header">
      <span>{{ t("chat.budgetPopover.title", "Token 用量预算") }}</span>
      <button
        type="button"
        class="budget-popover-reset"
        :disabled="budgetUnavailable || !budgetEnabled"
        data-testid="budget-reset-used"
        :title="t('chat.sessionTools.budget.resetUsedTitle', '将已用量清零')"
        @click="onResetUsed"
      >
        {{ t("chat.sessionTools.budget.resetUsed", "重置已用量") }}
      </button>
    </div>

    <div class="budget-popover-body">
      <div
        v-if="budgetUnavailable"
        class="budget-popover-empty"
        data-testid="budget-unavailable"
      >
        {{ t("chat.sessionTools.budget.unavailable", "发送首条消息后即可设置本会话预算。") }}
      </div>
      <template v-else>
        <div class="budget-row">
          <input
            v-model="budgetInput"
            type="number"
            inputmode="numeric"
            min="1"
            step="1"
            class="budget-input"
            data-testid="budget-input"
            :placeholder="t('chat.sessionTools.budget.placeholder', '不限')"
            :aria-label="t('chat.sessionTools.budget.inputAria', 'Token 预算上限（留空 = 不限）')"
            @keydown.enter.prevent="commitBudget"
            @blur="commitBudget"
          />
          <span class="budget-unit">{{ t("chat.sessionTools.budget.unit", "tokens") }}</span>
        </div>
        <div
          class="budget-usage"
          data-testid="budget-usage"
        >
          <template v-if="budgetEnabled && budgetSnapshot !== null && budgetSnapshot.max_tokens !== null">
            {{
              t("chat.sessionTools.budget.usage", {
                used: budgetSnapshot.used_tokens.toLocaleString(),
                max: budgetSnapshot.max_tokens.toLocaleString(),
              })
            }}
          </template>
          <template v-else>
            {{
              t("chat.sessionTools.budget.usageUnbounded", {
                used: (budgetSnapshot?.used_tokens ?? 0).toLocaleString(),
              })
            }}
          </template>
        </div>
      </template>

      <div class="budget-popover-foot">
        💡 {{ t("chat.budgetPopover.hint", "为整个会话（含子 / 孙 Agent）设置 Token 总用量上限；仅本会话生效，刷新后恢复默认。") }}
      </div>
    </div>
  </div>
</template>

<style scoped>
/* Popover container — reuses global theme tokens (no magic numbers / colours),
   matching the visual language of SessionToolsPopover. */
.budget-popover {
  position: absolute;
  bottom: calc(100% + 8px);
  right: 0;
  z-index: 50;
  min-width: 260px;
  max-width: 320px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  padding: 10px 12px 12px;
  animation: toast-in 0.12s ease-out;
}

.budget-popover-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 8px;
}

.budget-popover-reset {
  font-size: var(--text-xs);
  color: var(--accent);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 2px 4px;
}
.budget-popover-reset:disabled {
  color: var(--text-muted);
  cursor: default;
}

.budget-popover-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.budget-popover-empty {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.budget-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.budget-input {
  flex: 1 1 auto;
  min-width: 0;
  font-size: var(--text-xs);
  padding: 4px 8px;
  border-radius: var(--radius-sm, 6px);
  border: 1px solid var(--border-light);
  background: var(--bg-primary, transparent);
  color: var(--text-primary);
}
.budget-input:focus {
  outline: none;
  border-color: var(--accent);
}

.budget-unit {
  font-size: var(--text-xs);
  color: var(--text-muted);
  flex-shrink: 0;
}

.budget-usage {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.budget-popover-foot {
  margin-top: 10px;
  font-size: var(--text-xs);
  color: var(--text-muted);
  line-height: 1.4;
}
</style>
