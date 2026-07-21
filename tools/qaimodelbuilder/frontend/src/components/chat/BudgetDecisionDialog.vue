<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * BudgetDecisionDialog — interactive per-conversation TOKEN budget decision.
 *
 * When a conversation reaches its ``max_budget_tokens`` cap the backend ends the
 * turn with ``END(reason="budget_exceeded")`` carrying the decision metadata
 * (current usage, the cap that was hit, and the cap a "continue" would apply).
 * The composer opens THIS dialog so the user chooses, instead of the turn being
 * silently stopped:
 *   - "Continue" — the composer raises the cap to ``nextMax`` (current +
 *     ``raisePct``%) via ``PATCH .../budget`` and resends a continuation turn.
 *   - "Stop" — the turn stays stopped; the dialog just closes.
 *
 * Structurally a sibling of AwayQuestionAutoAnswerDialog / RenameDialog: reuses
 * the global ``.rename-dialog*`` classes + focus trap + Escape/backdrop guard.
 * AGENTS.md §3.9.2: project-defined dialog — native confirm/alert are forbidden.
 */
import { computed, ref, toRef } from "vue";
import { useI18n } from "vue-i18n";
import { useFocusTrap } from "@/composables/useFocusTrap";

interface Props {
  visible: boolean;
  /** Current cumulative usage that hit the cap. */
  used: number;
  /** The cap that was hit. */
  max: number;
  /** The cap a "continue" would apply (current + raisePct%). */
  nextMax: number;
  /** The percentage the cap is raised by on continue. */
  raisePct: number;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  /** Continue: raise the cap to `nextMax` and resend a continuation turn. */
  continue: [];
  /** Stop: leave the turn stopped. */
  stop: [];
}>();

const { t } = useI18n();
const dialogEl = ref<HTMLElement | null>(null);

useFocusTrap(dialogEl, { active: toRef(props, "visible"), focusFirst: true });

const usedText = computed(() => props.used.toLocaleString());
const maxText = computed(() => props.max.toLocaleString());
const nextMaxText = computed(() => props.nextMax.toLocaleString());

// Backdrop-dismiss guard (RenameDialog.vue parity): a dismiss must both start
// AND end on the overlay element so a drag-select never spuriously closes it.
// A backdrop dismiss is treated as "stop" (the conservative, non-spending choice).
const pointerDownOnOverlay = ref(false);

function onOverlayPointerDown(event: PointerEvent): void {
  pointerDownOnOverlay.value = event.target === event.currentTarget;
}

function onOverlayClick(event: MouseEvent): void {
  if (event.target === event.currentTarget && pointerDownOnOverlay.value) {
    emit("stop");
  }
  pointerDownOnOverlay.value = false;
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="rename-dialog-overlay"
      data-testid="budget-decision-overlay"
      @pointerdown="onOverlayPointerDown"
      @click="onOverlayClick"
    >
      <div
        ref="dialogEl"
        class="rename-dialog budget-decision-dialog"
        role="dialog"
        aria-modal="true"
        :aria-label="t('chat.budgetDecision.title')"
        data-testid="budget-decision-dialog"
        @keydown.esc="emit('stop')"
      >
        <div class="rename-dialog-title">
          ⚠️ {{ t("chat.budgetDecision.title") }}
        </div>
        <div class="budget-decision-desc">
          {{
            t("chat.budgetDecision.body", {
              used: usedText,
              max: maxText,
            })
          }}
        </div>
        <div class="budget-decision-notice">
          {{
            t("chat.budgetDecision.raiseNotice", {
              pct: raisePct,
              next: nextMaxText,
            })
          }}
        </div>

        <div class="rename-dialog-actions budget-decision-actions">
          <button
            type="button"
            class="rename-dialog-btn"
            data-testid="budget-decision-stop"
            @click="emit('stop')"
          >
            {{ t("chat.budgetDecision.stop") }}
          </button>
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--primary"
            data-testid="budget-decision-continue"
            @click="emit('continue')"
          >
            {{ t("chat.budgetDecision.continue", { pct: raisePct }) }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* Reuses the global `.rename-dialog*` tokens (overlay / dialog / title / actions
   / btn) — only the budget-specific spacing + the notice accent are local, all
   from theme vars (no hard-coded colours). */
.budget-decision-dialog {
  max-width: 380px;
}

.budget-decision-desc {
  font-size: var(--text-sm);
  color: var(--text-primary);
  line-height: 1.5;
  margin-bottom: 8px;
}

.budget-decision-notice {
  font-size: var(--text-xs);
  color: var(--text-muted);
  line-height: 1.5;
  padding: 6px 10px;
  border-radius: var(--radius-sm, 6px);
  border: 1px solid var(--border-light);
  background: var(--bg-primary, transparent);
  margin-bottom: 4px;
}

.budget-decision-actions {
  margin-top: 12px;
}
</style>
