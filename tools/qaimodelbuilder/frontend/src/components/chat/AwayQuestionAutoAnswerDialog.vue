<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * AwayQuestionAutoAnswerDialog — per-tab "auto-answer when away" settings modal
 * for the blocking `question` tool (V2 enhancement; no V1 equivalent).
 *
 * Before stepping away from the computer, the user opens this from the chat
 * composer's right-side toolbar and enables auto-answer FOR THE CURRENT
 * CONVERSATION (per-tab, default-off): when a `question` then blocks the agentic
 * loop, the ChatQuestionCard counts down `timeoutSeconds` and, on expiry, sends
 * `prompt` (or the current-locale default) to the model through the SAME
 * `answerQuestion` path as a manual answer — so the model keeps working instead
 * of stalling. This setting only ever affects the tab it was opened for.
 *
 * Structurally a sibling of ConversationWorkspaceDialog / RenameDialog: it
 * reuses the global `.rename-dialog*` classes + focus trap + Escape/backdrop
 * guard. AGENTS.md §3.9.2: project-defined dialog — native window.prompt /
 * confirm / alert are forbidden project-wide.
 */
import { ref, watch, toRef } from "vue";
import { useI18n } from "vue-i18n";
import { useFocusTrap } from "@/composables/useFocusTrap";
import {
  AWAY_AUTO_ANSWER_MIN_SECONDS,
  AWAY_AUTO_ANSWER_MAX_SECONDS,
  DEFAULT_AWAY_AUTO_ANSWER,
  type AwayAutoAnswerSettings,
} from "@/stores/_chatTabsTypes";

interface Props {
  visible: boolean;
  /** The current settings of the tab this dialog edits (undefined ⇒ defaults). */
  settings?: AwayAutoAnswerSettings;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  /** Persist the edited settings onto the owning tab. */
  save: [value: AwayAutoAnswerSettings];
  cancel: [];
}>();

const { t } = useI18n();
const dialogEl = ref<HTMLElement | null>(null);
const firstFieldRef = ref<HTMLInputElement | null>(null);

useFocusTrap(dialogEl, { active: toRef(props, "visible"), focusFirst: true });

// Local editable draft — committed only on Save (Cancel discards). Seeded from
// the tab's current settings each time the dialog opens.
const enabled = ref(DEFAULT_AWAY_AUTO_ANSWER.enabled);
const timeoutSeconds = ref(DEFAULT_AWAY_AUTO_ANSWER.timeoutSeconds);
const prompt = ref("");

function seedFromProps(): void {
  const s = props.settings ?? DEFAULT_AWAY_AUTO_ANSWER;
  enabled.value = s.enabled;
  timeoutSeconds.value = s.timeoutSeconds;
  // Empty stored prompt ⇒ prefill the current-locale default so the textarea is
  // never blank and the user sees exactly what would be sent.
  prompt.value = s.prompt.trim() !== "" ? s.prompt : t("chat.awayQuestionAutoAnswer.defaultPrompt");
}

watch(
  () => props.visible,
  (v) => {
    if (v) {
      seedFromProps();
      setTimeout(() => firstFieldRef.value?.focus(), 0);
    }
  },
  { immediate: true },
);

function restoreDefault(): void {
  prompt.value = t("chat.awayQuestionAutoAnswer.defaultPrompt");
}

function clampTimeout(): void {
  let n = Number(timeoutSeconds.value);
  if (!Number.isFinite(n)) n = DEFAULT_AWAY_AUTO_ANSWER.timeoutSeconds;
  n = Math.min(AWAY_AUTO_ANSWER_MAX_SECONDS, Math.max(AWAY_AUTO_ANSWER_MIN_SECONDS, Math.round(n)));
  timeoutSeconds.value = n;
}

function onSave(): void {
  clampTimeout();
  // Persist "" (NOT the literal default text) when the user left the textarea
  // at the locale default: the store keeps an empty prompt and the send path
  // substitutes the CURRENT-locale default at fire time — so switching the UI
  // language later keeps the auto-answer in the active language instead of
  // freezing whatever language was shown when the dialog was opened. A custom
  // (edited) prompt is stored verbatim.
  const trimmed = prompt.value.trim();
  const localeDefault = t("chat.awayQuestionAutoAnswer.defaultPrompt").trim();
  emit("save", {
    enabled: enabled.value,
    timeoutSeconds: timeoutSeconds.value,
    prompt: trimmed === localeDefault ? "" : trimmed,
  });
}

// Backdrop-dismiss guard (RenameDialog.vue parity): a dismiss must both start
// AND end on the overlay element so a drag-select never spuriously closes it.
const pointerDownOnOverlay = ref(false);

function onOverlayPointerDown(event: PointerEvent): void {
  pointerDownOnOverlay.value = event.target === event.currentTarget;
}

function onOverlayClick(event: MouseEvent): void {
  if (event.target === event.currentTarget && pointerDownOnOverlay.value) {
    emit("cancel");
  }
  pointerDownOnOverlay.value = false;
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="rename-dialog-overlay"
      @pointerdown="onOverlayPointerDown"
      @click="onOverlayClick"
    >
      <div
        ref="dialogEl"
        class="rename-dialog away-auto-answer-dialog"
        role="dialog"
        aria-modal="true"
        :aria-label="t('chat.awayQuestionAutoAnswer.title')"
        @keydown.esc="emit('cancel')"
      >
        <div class="rename-dialog-title">
          ⏰ {{ t("chat.awayQuestionAutoAnswer.title") }}
        </div>
        <div class="away-auto-answer-desc">
          {{ t("chat.awayQuestionAutoAnswer.description") }}
        </div>

        <label class="away-auto-answer-row away-auto-answer-row--toggle">
          <input
            ref="firstFieldRef"
            type="checkbox"
            class="away-auto-answer-checkbox"
            :checked="enabled"
            data-testid="away-auto-answer-enable"
            @change="enabled = ($event.target as HTMLInputElement).checked"
          />
          <span>{{ t("chat.awayQuestionAutoAnswer.enableLabel") }}</span>
        </label>

        <label class="away-auto-answer-field">
          <span class="away-auto-answer-label">
            {{ t("chat.awayQuestionAutoAnswer.timeoutLabel") }}
          </span>
          <input
            type="number"
            class="rename-dialog-input away-auto-answer-number"
            :min="AWAY_AUTO_ANSWER_MIN_SECONDS"
            :max="AWAY_AUTO_ANSWER_MAX_SECONDS"
            :value="timeoutSeconds"
            data-testid="away-auto-answer-timeout"
            @input="timeoutSeconds = Number(($event.target as HTMLInputElement).value)"
            @blur="clampTimeout"
          />
        </label>

        <label class="away-auto-answer-field">
          <span class="away-auto-answer-label">
            {{ t("chat.awayQuestionAutoAnswer.promptLabel") }}
          </span>
          <textarea
            v-model="prompt"
            class="rename-dialog-input away-auto-answer-textarea"
            rows="6"
            :placeholder="t('chat.awayQuestionAutoAnswer.promptPlaceholder')"
            data-testid="away-auto-answer-prompt"
          />
        </label>

        <div class="rename-dialog-footer away-auto-answer-footer">
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--cancel"
            @click="restoreDefault"
          >
            {{ t("chat.awayQuestionAutoAnswer.restoreDefault") }}
          </button>
          <span class="away-auto-answer-footer-spacer" />
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--cancel"
            @click="emit('cancel')"
          >
            {{ t("chat.awayQuestionAutoAnswer.cancel") }}
          </button>
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--confirm"
            data-testid="away-auto-answer-save"
            @click="onSave"
          >
            {{ t("chat.awayQuestionAutoAnswer.save") }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* Wider than the single-line rename dialog: this form has a multi-line prompt
   textarea. Theme tokens only (AGENTS.md §3.10). */
.away-auto-answer-dialog {
  width: min(560px, 92vw);
}
.away-auto-answer-desc {
  margin: -4px 0 14px;
  font-size: var(--text-xs);
  line-height: 1.6;
  color: var(--text-secondary);
}
.away-auto-answer-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.away-auto-answer-row--toggle {
  margin-bottom: 14px;
  cursor: pointer;
  font-size: var(--text-sm);
  color: var(--text-primary);
}
.away-auto-answer-checkbox {
  width: 16px;
  height: 16px;
  cursor: pointer;
}
.away-auto-answer-field {
  display: block;
  margin-bottom: 14px;
}
.away-auto-answer-label {
  display: block;
  margin-bottom: 6px;
  font-size: var(--text-xs);
  color: var(--text-secondary);
}
.away-auto-answer-number {
  width: 140px;
}
.away-auto-answer-textarea {
  width: 100%;
  resize: vertical;
  line-height: 1.6;
  font-family: inherit;
}
.away-auto-answer-footer {
  display: flex;
  align-items: center;
  gap: 8px;
}
.away-auto-answer-footer-spacer {
  flex: 1 1 auto;
}
</style>
