<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * PromptEnhanceBtn — input-toolbar "enhance prompt" button.
 *
 * Block 1 (P4-A redo): rebuilt to match V1
 * (`frontend/js/components/PromptEnhanceBtn.js`, 189 lines):
 *   - Icon-only `.rit-btn .rit-prompt-enhance` pill (no "*" text label),
 *     same 28×28 footprint as the voice button, using design tokens
 *     (`--accent` / `--border`) shared with the rest of the toolbar.
 *   - Three states:
 *       idle      → sparkle-wand SVG, enabled when text is non-empty
 *       enhancing → spinning arc SVG, disabled
 *       undo      → for `undoWindowMs` (8s) after a successful enhance,
 *                   the icon becomes an undo arrow; clicking restores
 *                   the pre-enhance text. The window closes early if the
 *                   user edits the enhanced text.
 *   - Failures surface via a brief shake + tooltip (no toast), keeping
 *     the existing V2 UX rule for predictable input failures.
 */
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { usePromptEnhance } from "@/composables/usePromptEnhance";
import { useChatTabsStore } from "@/stores/chatTabs";

interface Props {
  /** Current text to enhance. */
  text: string;
  /**
   * Legacy prop kept for backwards compatibility — the backend's
   * `/api/prompt/enhance` route uses a server-side default LLM when
   * the client omits `model_id`, so the button no longer needs a
   * locally-configured model to enable. The prop is accepted but
   * intentionally ignored for the enable check.
   */
  hasModels?: boolean;
  /** Undo window in ms after a successful enhance. V1 default = 8000. */
  undoWindowMs?: number;
}

const props = withDefaults(defineProps<Props>(), {
  hasModels: false,
  undoWindowMs: 8000,
});

const emit = defineEmits<{
  enhanced: [text: string];
}>();

const { t } = useI18n();
const { enhancing, lastFailed, canUndo, enhance, undo } = usePromptEnhance();
const store = useChatTabsStore();

const shake = ref(false);
const showFailTooltip = ref(false);

// Local "undo is offered" window. Driven by composable `canUndo` but with
// an auto-expiry timer (V1 parity: undoVisible + _undoTimer).
const undoVisible = ref(false);
let undoTimer: ReturnType<typeof setTimeout> | null = null;

function clearUndoTimer(): void {
  if (undoTimer !== null) {
    clearTimeout(undoTimer);
    undoTimer = null;
  }
}

watch(canUndo, (now) => {
  clearUndoTimer();
  if (now) {
    undoVisible.value = true;
    undoTimer = setTimeout(() => {
      undoVisible.value = false;
      undoTimer = null;
    }, Math.max(2000, props.undoWindowMs));
  } else {
    undoVisible.value = false;
  }
});

onBeforeUnmount(clearUndoTimer);

// Enabled whenever there is non-empty text and no in-flight call; in the
// undo window the button is always clickable (undo needs no content).
const canEnhance = computed(
  () => !enhancing.value && props.text.trim() !== "",
);
const isDisabled = computed(() => {
  if (enhancing.value) return true;
  if (undoVisible.value) return false;
  return !canEnhance.value;
});

// When `lastFailed` flips to true, briefly shake the button and show
// the failure tooltip; clear after a short window.
watch(lastFailed, (failed) => {
  if (!failed) return;
  shake.value = true;
  showFailTooltip.value = true;
  window.setTimeout(() => {
    shake.value = false;
  }, 500);
  window.setTimeout(() => {
    showFailTooltip.value = false;
  }, 2500);
});

async function handleClick(): Promise<void> {
  if (isDisabled.value) return;
  // Undo state takes precedence.
  if (undoVisible.value) {
    const restored = undo(props.text);
    if (restored !== null) {
      emit("enhanced", restored);
      undoVisible.value = false;
      clearUndoTimer();
    }
    return;
  }
  const tab = store.activeTab;
  const modelId = tab?.modelId;
  const result = await enhance(props.text, modelId);
  if (!lastFailed.value && result !== props.text) {
    emit("enhanced", result);
  }
}

const tooltip = computed(() => {
  if (showFailTooltip.value) return t("chat.enhanceFailed");
  if (enhancing.value) {
    return t("promptEnhance.tooltip.enhancing", "Enhancing prompt…");
  }
  if (undoVisible.value) {
    return t("promptEnhance.tooltip.undo", "Click to undo and restore the original prompt");
  }
  if (!canEnhance.value) {
    return t("promptEnhance.tooltip.empty", "Type a prompt first to enhance");
  }
  return t("promptEnhance.tooltip.idle", "Enhance prompt");
});

const ariaLabel = computed(() =>
  undoVisible.value
    ? t("promptEnhance.aria.undo", "Undo prompt enhancement")
    : t("promptEnhance.aria.label", "Enhance prompt"),
);
</script>

<template>
  <button
    type="button"
    class="rit-btn rit-prompt-enhance"
    :class="{
      'rit-prompt-enhance--loading': enhancing,
      'rit-prompt-enhance--undo': undoVisible && !enhancing,
      'rit-prompt-enhance--shake': shake,
      'rit-prompt-enhance--failed': showFailTooltip,
    }"
    :disabled="isDisabled"
    :title="tooltip"
    :aria-label="ariaLabel"
    :aria-busy="enhancing ? 'true' : 'false'"
    data-testid="prompt-enhance-btn"
    @click="() => { void handleClick(); }"
  >
    <!-- idle: sparkle wand -->
    <svg
      v-if="!enhancing && !undoVisible"
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M15 4 L20 9" />
      <path d="M4 20 L17 7" />
      <path d="M5 5 v3 M3.5 6.5 h3" />
      <path d="M19 14 v2 M18 15 h2" />
      <path d="M11 3 v2 M10 4 h2" />
    </svg>

    <!-- loading: spinning arc -->
    <svg
      v-else-if="enhancing"
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      class="rit-prompt-enhance__spin"
      aria-hidden="true"
    >
      <path d="M21 12a9 9 0 1 1-6.219-8.56" />
    </svg>

    <!-- undo: left-curving arrow (8s window after a successful enhance) -->
    <svg
      v-else
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M3 7v6h6" />
      <path d="M21 17a9 9 0 0 0 -15 -6.7L3 13" />
    </svg>
  </button>
</template>

<style scoped>
/* The base .rit-btn pill styling is supplied by chat.css; this scope
 * only adds the enhance-specific state colours + the spinner/shake. */
.rit-prompt-enhance--undo {
  color: var(--accent, #a594ff);
  border-color: var(--accent, #a594ff);
}

.rit-prompt-enhance--failed {
  border-color: var(--error, #ef4444);
  color: var(--error, #ef4444);
}

.rit-prompt-enhance--shake {
  animation: prompt-enhance-shake 0.4s ease-in-out;
}

.rit-prompt-enhance__spin {
  animation: prompt-enhance-spin 0.6s linear infinite;
  transform-origin: center;
}

@keyframes prompt-enhance-spin {
  to {
    transform: rotate(360deg);
  }
}

@keyframes prompt-enhance-shake {
  0%, 100% { transform: translateX(0); }
  20%      { transform: translateX(-3px); }
  40%      { transform: translateX(3px); }
  60%      { transform: translateX(-2px); }
  80%      { transform: translateX(2px); }
}
</style>
