<script setup lang="ts">
// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * ForkConversationDialog — modal for forking (branching) a conversation.
 *
 * Two entry modes:
 *   `entryMode: "conversation"` — opened from the sidebar's 🔀 button
 *      (fork the whole conversation; rounds/messages slicers offered).
 *   `entryMode: "message"` — opened from a specific message's 🔀 button
 *      (fork up to that message; `upToMessageId` is fixed).
 *
 * The dialog defaults to a rounds-based slicer with "All" selected. An
 * advanced panel folds up the raw-message-count slicers (message-fork
 * mode hides that section entirely since the anchor is a specific
 * message id).
 *
 * Visual styling reuses the global `.rename-dialog*` classes from
 * `styles/components/components.css` (V1-parity dialog shell). Custom
 * additions are scoped. AGENTS.md §3.9.2: project-defined dialog.
 */
import { computed, ref, toRef, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useFocusTrap } from "@/composables/useFocusTrap";

export type ForkEntryMode = "conversation" | "message";
export type ForkMode =
  | "all"
  | "rounds-first"
  | "rounds-last"
  | "messages-first"
  | "messages-last"
  | "up-to-message";

export interface ForkConfirmPayload {
  mode: ForkMode;
  count: number | null;
  upToMessageId: string | null;
  title: string;
  includeToolCalls: boolean;
  inheritSettings: boolean;
}

const props = withDefaults(
  defineProps<{
    visible: boolean;
    conversationId: string;
    /** Total assistant/user round count in the source. */
    roundCount: number;
    /** Total raw message count in the source. */
    messageCount: number;
    /** Source conversation title (used to build the default fork title). */
    sourceTitle?: string;
    /** "conversation" (sidebar) or "message" (per-message button). */
    entryMode?: ForkEntryMode;
    /** Fixed anchor when `entryMode === "message"`. */
    upToMessageId?: string | null;
    /**
     * 1-based round index of the anchor message. In message-mode, this
     * limits "最近 N 轮" max to the anchor's position (can't select more
     * rounds than exist up to and including the anchor). Ignored in
     * conversation mode. Default 0 = unknown (falls back to roundCount).
     */
    anchorRoundIndex?: number;
    /**
     * Optional per-round message counts (index i = messages in round i).
     * When omitted the preview falls back to a rounds-only string.
     */
    messagesPerRound?: number[];
  }>(),
  {
    sourceTitle: "",
    entryMode: "conversation",
    upToMessageId: null,
    anchorRoundIndex: 0,
    messagesPerRound: () => [],
  },
);

const emit = defineEmits<{
  (e: "cancel"): void;
  (e: "confirm", payload: ForkConfirmPayload): void;
}>();

const { t } = useI18n();

// Selected slicer mode. In conversation mode, defaults to `all`; in
// message-fork mode the anchor is a fixed message id, so "all" here means
// "everything up to and including the anchor" (the backend already treats
// `up_to_message_id` that way — see fork_conversation.py).
const mode = ref<ForkMode>("all");
const roundsN = ref<number>(3);
const messagesN = ref<number>(10);
const advancedOpen = ref<boolean>(false);
const title = ref<string>("");
const includeToolCalls = ref<boolean>(false);
const inheritSettings = ref<boolean>(true);

const dialogEl = ref<HTMLElement | null>(null);
useFocusTrap(dialogEl, { active: toRef(props, "visible"), focusFirst: true, onEscape: () => emit("cancel") });

// Reset state each time the dialog opens so it never surfaces a stale
// selection from a previous fork (matches RenameDialog's reset-on-open).
watch(
  () => props.visible,
  (v) => {
    if (!v) return;
    mode.value = "all";
    roundsN.value = Math.max(1, Math.min(3, props.roundCount || 1));
    messagesN.value = Math.max(1, Math.min(10, props.messageCount || 1));
    advancedOpen.value = false;
    includeToolCalls.value = false;
    inheritSettings.value = true;
    // Default title: "<source> (fork)" for conversation-mode; plain source
    // title for message-fork (message anchor already implies "branched at").
    const base = props.sourceTitle ?? "";
    title.value =
      props.entryMode === "message" ? base : base ? `${base} (fork)` : "";
  },
);

const isMessageMode = computed<boolean>(() => props.entryMode === "message");

// ── Preview computation ─────────────────────────────────────────────────────
// Sums messagesPerRound[0..n) or [len-n..len). Falls back to raw N when the
// per-round breakdown isn't provided (parent may not have loaded it yet).
function _sumFirstRounds(n: number): number {
  const per = props.messagesPerRound;
  if (per.length === 0) return 0;
  const cap = Math.min(n, per.length);
  let sum = 0;
  for (let i = 0; i < cap; i++) sum += per[i] ?? 0;
  return sum;
}
function _sumLastRounds(n: number): number {
  const per = props.messagesPerRound;
  if (per.length === 0) return 0;
  const cap = Math.min(n, per.length);
  let sum = 0;
  for (let i = per.length - cap; i < per.length; i++) sum += per[i] ?? 0;
  return sum;
}

const previewText = computed<string>(() => {
  const hasBreakdown = props.messagesPerRound.length > 0;

  if (mode.value === "all") {
    if (isMessageMode.value) {
      // "All rounds up to here" — we don't have a precise round-count for the
      // anchor without walking messages, so keep it conservative.
      return t("chat.fork.previewRoundsOnly", { rounds: props.roundCount });
    }
    if (hasBreakdown) {
      return t("chat.fork.previewRounds", {
        rounds: props.roundCount,
        messages: props.messageCount,
      });
    }
    return t("chat.fork.previewRoundsOnly", { rounds: props.roundCount });
  }
  if (mode.value === "rounds-first") {
    const n = Math.max(0, roundsN.value | 0);
    if (hasBreakdown) {
      return t("chat.fork.previewRounds", {
        rounds: n,
        messages: _sumFirstRounds(n),
      });
    }
    return t("chat.fork.previewRoundsOnly", { rounds: n });
  }
  if (mode.value === "rounds-last") {
    const n = Math.max(0, roundsN.value | 0);
    if (hasBreakdown) {
      return t("chat.fork.previewRounds", {
        rounds: n,
        messages: _sumLastRounds(n),
      });
    }
    return t("chat.fork.previewRoundsOnly", { rounds: n });
  }
  // Raw-message advanced modes
  return t("chat.fork.previewMessages", {
    messages: Math.max(0, messagesN.value | 0),
  });
});

const canConfirm = computed<boolean>(() => {
  if (mode.value === "all") return true;
  if (mode.value === "rounds-first" || mode.value === "rounds-last") {
    return roundsN.value > 0 && roundsN.value <= Math.max(1, props.roundCount);
  }
  if (mode.value === "messages-first" || mode.value === "messages-last") {
    return (
      messagesN.value > 0 && messagesN.value <= Math.max(1, props.messageCount)
    );
  }
  return false;
});

function onConfirm(): void {
  if (!canConfirm.value) return;

  // Resolve the emitted mode + count. In message-fork mode the anchor is the
  // message id; the `mode`/`count` fields still describe how many rounds to
  // keep BEFORE the anchor (backend enforces mutual exclusion, so we send
  // `up-to-message` as the primary mode and drop count when "all" is picked).
  let outMode: ForkMode = mode.value;
  let outCount: number | null = null;

  if (mode.value === "all") {
    outMode = isMessageMode.value ? "up-to-message" : "all";
    outCount = null;
  } else if (
    mode.value === "rounds-first" ||
    mode.value === "rounds-last"
  ) {
    outCount = roundsN.value;
  } else {
    outCount = messagesN.value;
  }

  const trimmedTitle = title.value.trim().slice(0, 256);

  emit("confirm", {
    mode: outMode,
    count: outCount,
    upToMessageId: isMessageMode.value ? props.upToMessageId ?? null : null,
    title: trimmedTitle,
    includeToolCalls: includeToolCalls.value,
    inheritSettings: inheritSettings.value,
  });
}

// ── Backdrop dismiss guard (same as RenameDialog) ────────────────────────────
let pointerDownOnOverlay = false;
function onOverlayPointerDown(ev: PointerEvent): void {
  pointerDownOnOverlay = ev.target === ev.currentTarget;
}
function onOverlayClick(ev: MouseEvent): void {
  if (pointerDownOnOverlay && ev.target === ev.currentTarget) {
    emit("cancel");
  }
  pointerDownOnOverlay = false;
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
        class="rename-dialog fork-dialog"
        role="dialog"
        aria-modal="true"
        :aria-label="isMessageMode ? t('chat.fork.titleFromMessage') : t('chat.fork.title')"
      >
        <div class="rename-dialog-title">
          <svg class="fork-dialog-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><path d="M12 15V12a3 3 0 0 0-3-3H9M12 12a3 3 0 0 1 3-3h0"/></svg> {{ isMessageMode ? t("chat.fork.titleFromMessage") : t("chat.fork.title") }}
        </div>
        <div class="rename-dialog-subtitle">
          {{ t("chat.fork.subtitle") }}
        </div>

        <!-- Title editor -->
        <label class="fork-field">
          <span class="fork-field-label">{{ t("chat.fork.titleLabel") }}</span>
          <input
            v-model="title"
            type="text"
            class="rename-dialog-input"
            :placeholder="t('chat.fork.titlePlaceholder')"
            :maxlength="256"
            data-testid="fork-title-input"
          />
        </label>

        <!-- Rounds slicer (primary) -->
        <div class="fork-section">
          <div class="fork-field-label">
            {{ t("chat.fork.scopeLabel") }}
          </div>
          <div class="fork-modes">
            <label class="fork-mode-option">
              <input
                v-model="mode"
                type="radio"
                name="fork-mode"
                value="all"
              />
              <span>{{ t("chat.fork.modeAllRounds") }}</span>
            </label>
            <label
              v-if="!isMessageMode"
              class="fork-mode-option"
            >
              <input
                v-model="mode"
                type="radio"
                name="fork-mode"
                value="rounds-first"
              />
              <span>{{ t("chat.fork.modeFirstRounds") }}</span>
              <input
                v-model.number="roundsN"
                type="number"
                class="rename-dialog-input fork-count-input"
                :min="1"
                :max="Math.max(1, roundCount)"
                :disabled="mode !== 'rounds-first'"
                data-testid="fork-first-rounds"
              />
              <span class="fork-unit">{{ t("chat.fork.roundsUnit") }}</span>
            </label>
            <label class="fork-mode-option">
              <input
                v-model="mode"
                type="radio"
                name="fork-mode"
                value="rounds-last"
              />
              <span>{{ t("chat.fork.modeLastRounds") }}</span>
              <input
                v-model.number="roundsN"
                type="number"
                class="rename-dialog-input fork-count-input"
                :min="1"
                :max="Math.max(1, isMessageMode && anchorRoundIndex > 0 ? anchorRoundIndex : roundCount)"
                :disabled="mode !== 'rounds-last'"
                data-testid="fork-last-rounds"
              />
              <span class="fork-unit">{{ t("chat.fork.roundsUnit") }}</span>
            </label>
          </div>
          <div
            v-if="isMessageMode"
            class="fork-note"
          >
            {{ t("chat.fork.messageAnchorNote") }}
          </div>
        </div>

        <!-- Advanced panel (raw message counts) — hidden entirely in
             message-fork mode since the anchor is a fixed message id. -->
        <div
          v-if="!isMessageMode"
          class="fork-advanced"
        >
          <button
            type="button"
            class="fork-advanced-toggle"
            :aria-expanded="advancedOpen"
            data-testid="fork-advanced-toggle"
            @click="advancedOpen = !advancedOpen"
          >
            <span class="fork-advanced-caret">{{ advancedOpen ? "▾" : "▸" }}</span>
            {{ t("chat.fork.advancedToggle") }}
          </button>
          <div
            v-if="advancedOpen"
            class="fork-advanced-body"
          >
            <label class="fork-mode-option">
              <input
                v-model="mode"
                type="radio"
                name="fork-mode"
                value="messages-first"
              />
              <span>{{ t("chat.fork.modeFirstMessages") }}</span>
              <input
                v-model.number="messagesN"
                type="number"
                class="rename-dialog-input fork-count-input"
                :min="1"
                :max="Math.max(1, messageCount)"
                :disabled="mode !== 'messages-first'"
                data-testid="fork-first-messages"
              />
              <span class="fork-unit">{{ t("chat.fork.messagesUnit") }}</span>
            </label>
            <label class="fork-mode-option">
              <input
                v-model="mode"
                type="radio"
                name="fork-mode"
                value="messages-last"
              />
              <span>{{ t("chat.fork.modeLastMessages") }}</span>
              <input
                v-model.number="messagesN"
                type="number"
                class="rename-dialog-input fork-count-input"
                :min="1"
                :max="Math.max(1, messageCount)"
                :disabled="mode !== 'messages-last'"
                data-testid="fork-last-messages"
              />
              <span class="fork-unit">{{ t("chat.fork.messagesUnit") }}</span>
            </label>
          </div>
        </div>

        <!-- Behavior toggles -->
        <label class="fork-toggle">
          <input
            v-model="includeToolCalls"
            type="checkbox"
            data-testid="fork-include-tool-calls"
          />
          <span class="fork-toggle-body">
            <span>{{ t("chat.fork.includeToolCalls") }}</span>
            <span class="fork-hint">{{ t("chat.fork.includeToolCallsHint") }}</span>
          </span>
        </label>

        <label class="fork-toggle">
          <input
            v-model="inheritSettings"
            type="checkbox"
            data-testid="fork-inherit-settings"
          />
          <span class="fork-toggle-body">
            <span>{{ t("chat.fork.inheritSettings") }}</span>
          </span>
        </label>

        <!-- Live preview -->
        <div
          class="fork-preview"
          data-testid="fork-preview"
        >
          {{ previewText }}
          <span
            v-if="!includeToolCalls"
            class="fork-preview-filter"
          >{{ t("chat.fork.previewFilterHint") }}</span>
        </div>

        <div class="rename-dialog-footer">
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--cancel"
            @click="emit('cancel')"
          >
            {{ t("chat.fork.cancel") }}
          </button>
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--confirm"
            :disabled="!canConfirm"
            data-testid="fork-confirm"
            @click="onConfirm"
          >
            {{ t("chat.fork.confirm") }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* Fork-specific layout on top of the shared .rename-dialog* shell. */
.fork-dialog {
  width: min(460px, 92vw);
}
.fork-field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1, 4px);
  margin-bottom: var(--space-3, 12px);
}
.fork-field-label {
  font-size: var(--text-sm, 13px);
  color: var(--text-secondary, #aaa);
}
.fork-section {
  margin-bottom: var(--space-3, 12px);
}
.fork-modes {
  display: flex;
  flex-direction: column;
  gap: var(--space-2, 8px);
  margin-top: var(--space-1, 4px);
}
.fork-mode-option {
  display: flex;
  align-items: center;
  gap: var(--space-2, 8px);
  cursor: pointer;
  font-size: var(--text-sm, 14px);
  color: var(--text-primary, #e0e0e0);
}
.fork-mode-option input[type="radio"] {
  accent-color: var(--accent, #4f8cff);
}
.fork-count-input {
  width: 72px;
  margin-bottom: 0;
}
.fork-count-input:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.fork-unit {
  font-size: var(--text-sm, 13px);
  color: var(--text-secondary, #aaa);
}
.fork-note {
  margin-top: var(--space-1, 4px);
  font-size: var(--text-xs, 12px);
  color: var(--text-secondary, #aaa);
  font-style: italic;
}
.fork-advanced {
  border-top: 1px solid var(--border-subtle, rgba(255, 255, 255, 0.08));
  padding-top: var(--space-2, 8px);
  margin-bottom: var(--space-3, 12px);
}
.fork-advanced-toggle {
  background: none;
  border: none;
  color: var(--text-secondary, #aaa);
  cursor: pointer;
  padding: 0;
  font-size: var(--text-sm, 13px);
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.fork-advanced-toggle:hover {
  color: var(--text-primary, #e0e0e0);
}
.fork-advanced-caret {
  display: inline-block;
  width: 12px;
  text-align: center;
}
.fork-advanced-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2, 8px);
  margin-top: var(--space-2, 8px);
  padding-left: var(--space-2, 8px);
  border-left: 2px solid var(--border-subtle, rgba(255, 255, 255, 0.08));
}
.fork-toggle {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2, 8px);
  cursor: pointer;
  margin-bottom: var(--space-2, 8px);
  font-size: var(--text-sm, 14px);
  color: var(--text-primary, #e0e0e0);
}
.fork-toggle input[type="checkbox"] {
  accent-color: var(--accent, #4f8cff);
  margin-top: 2px;
}
.fork-toggle-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.fork-hint {
  font-size: var(--text-xs, 12px);
  color: var(--text-secondary, #aaa);
}
.fork-preview {
  margin: var(--space-3, 12px) 0;
  padding: var(--space-2, 8px);
  background: var(--bg-subtle, rgba(255, 255, 255, 0.04));
  border-radius: var(--radius-sm, 4px);
  font-size: var(--text-sm, 13px);
  color: var(--text-primary, #e0e0e0);
}
.fork-preview-filter {
  margin-left: var(--space-2, 8px);
  font-size: var(--text-xs, 12px);
  color: var(--text-secondary, #aaa);
  font-style: italic;
}
</style>
