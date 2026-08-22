<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * VoiceInputBtn — microphone button (T2.6-A real port).
 *
 * Three states drive the icon + style:
 *   - idle       → microphone glyph
 *   - recording  → red pulsing border, time counter
 *   - processing → animated dots
 *   - error      → shake animation + tooltip
 *
 * The transcript ref from `useVoiceInput` is watched and emitted upward
 * via the `transcribed` event so the parent (ChatComposer) can append
 * it to the textarea.
 */
import { computed, ref, watch, onBeforeUnmount } from "vue";
import { useI18n } from "vue-i18n";
import { useVoiceInput } from "@/composables/useVoiceInput";
import { useTranscribedForwarding } from "@/composables/chat/useTranscribedForwarding";

const emit = defineEmits<{
  transcribed: [text: string];
}>();

const { t } = useI18n();
const {
  state,
  isListening,
  isProcessing,
  isBusy,
  recordSecs,
  transcript,
  partialTranscript,
  errorText,
  available,
  toggle,
  cancel,
} = useVoiceInput();

const shake = ref(false);

// Forward both the end-of-recording transcript and the interim streaming
// chunks to the composer. The forwarding contract — in particular the
// identical-chunk handling that the original `text !== prev` guard got
// wrong (two consecutive interim chunks recognising the SAME phrase were
// silently dropped) — lives in a dedicated, unit-tested composable. See
// `useTranscribedForwarding` for the full rationale.
const forwarding = useTranscribedForwarding(
  { transcript, partialTranscript },
  { onTranscribed: (text) => emit("transcribed", text) },
);
onBeforeUnmount(() => forwarding.stop());

// Visually indicate transient errors with a shake; keep the error state
// readable via the title attribute (no toast).
watch(state, (next, prev) => {
  if (next === "error" && prev !== "error") {
    shake.value = true;
    window.setTimeout(() => {
      shake.value = false;
    }, 500);
  }
});

function formatTime(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

const tooltip = computed(() => {
  if (!available.value) {
    return t("voiceInput.unsupportedTitle");
  }
  if (state.value === "error" && errorText.value !== "") {
    return errorText.value;
  }
  if (isProcessing.value) {
    return t("voiceInput.processingTitle");
  }
  if (isListening.value) {
    return t("voiceInput.stopTitle");
  }
  return t("voiceInput.startTitle");
});

function onContextMenu(ev: MouseEvent): void {
  // Right-click cancels — mirrors V1 behaviour.
  if (isBusy.value) {
    ev.preventDefault();
    cancel();
  }
}
</script>

<template>
  <button
    type="button"
    class="voice-input-btn"
    :class="{
      'voice-input-btn--listening': isListening,
      'voice-input-btn--processing': isProcessing,
      'voice-input-btn--unavailable': !available,
      'voice-input-btn--shake': shake,
      'voice-input-btn--error': state === 'error',
    }"
    :title="tooltip"
    :aria-label="tooltip"
    data-testid="voice-input-btn"
    @click="toggle"
    @contextmenu="onContextMenu"
  >
    <svg
      v-if="state === 'idle' || state === 'error'"
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line
        x1="12"
        y1="19"
        x2="12"
        y2="23"
      />
      <line
        x1="8"
        y1="23"
        x2="16"
        y2="23"
      />
    </svg>
    <span
      v-else-if="isListening"
      class="voice-input-btn__rec-dot"
      aria-hidden="true"
    />
    <span
      v-else
      class="voice-input-btn__spinner"
      aria-hidden="true"
    />

    <span
      v-if="isListening"
      class="voice-input-btn__time"
    >
      {{ formatTime(recordSecs) }}
    </span>
    <span
      v-else-if="isProcessing"
      class="voice-input-btn__label"
    >
      {{ t("voiceInput.phase.processing") }}
      <span class="voice-input-btn__cancel-hint" aria-hidden="true">&times;</span>
    </span>
  </button>
</template>

<style scoped>
.voice-input-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1, 4px);
  padding: 5px 8px;
  border: 1px solid var(--border, #2a2750);
  border-radius: var(--radius-sm, 8px);
  background: transparent;
  color: var(--text-secondary, #b6b8d6);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-sm, 12px);
  transition: border-color 0.15s, color 0.15s, transform 0.05s;
}

.voice-input-btn:hover:not(:disabled) {
  border-color: var(--accent, #a594ff);
  color: var(--accent, #a594ff);
}

.voice-input-btn--listening {
  border-color: #ef4444;
  color: #ef4444;
  background: rgba(239, 68, 68, 0.08);
  animation: voice-pulse 1.4s ease-in-out infinite;
}

.voice-input-btn--processing {
  border-color: var(--accent, #a594ff);
  color: var(--accent, #a594ff);
  cursor: pointer;
}

/* V1 parity: the mic button stays CLICKABLE even when unavailable —
   clicking surfaces a warning toast ("only supported on Chrome/Edge" or
   "permission denied") instead of being inert. So `--unavailable` only
   dims it; it must NOT set `cursor: not-allowed` or block pointer events. */
.voice-input-btn--unavailable {
  opacity: 0.55;
}
.voice-input-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.voice-input-btn--error {
  border-color: var(--error, #ef4444);
  color: var(--error, #ef4444);
}

.voice-input-btn--shake {
  animation: voice-shake 0.4s ease-in-out;
}

.voice-input-btn__time {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
}

.voice-input-btn__label {
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #6b7280);
}

.voice-input-btn__cancel-hint {
  margin-left: 2px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary, #b6b8d6);
  opacity: 0.7;
  transition: opacity 0.15s;
}
.voice-input-btn:hover .voice-input-btn__cancel-hint {
  opacity: 1;
  color: var(--error, #ef4444);
}

.voice-input-btn__rec-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #ef4444;
  display: inline-block;
}

.voice-input-btn__spinner {
  width: 10px;
  height: 10px;
  border: 2px solid var(--border, #2a2750);
  border-top-color: var(--accent, #a594ff);
  border-radius: 50%;
  animation: voice-spin 0.6s linear infinite;
}

@keyframes voice-pulse {
  0%, 100% {
    box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.45);
  }
  50% {
    box-shadow: 0 0 0 4px rgba(239, 68, 68, 0);
  }
}

@keyframes voice-shake {
  0%, 100% {
    transform: translateX(0);
  }
  20% {
    transform: translateX(-3px);
  }
  40% {
    transform: translateX(3px);
  }
  60% {
    transform: translateX(-2px);
  }
  80% {
    transform: translateX(2px);
  }
}

@keyframes voice-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
