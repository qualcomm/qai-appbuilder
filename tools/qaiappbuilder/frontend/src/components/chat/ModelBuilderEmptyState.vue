<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModelBuilderEmptyState — the mode-specific welcome screen shown in「Model
 * Builder」chat when there are no messages yet. Replaces the generic welcome
 * with a 3-step guide + ready-to-send example prompts so the user knows what
 * to do (convert / quantize a source model → validate on the NPU → optionally
 * promote to App Builder).
 *
 * Design parity: mirrors CodeEmptyState / ModelHubEmptyState so the mode
 * welcome screens read as one family. Onboarding copy is the single source of
 * truth in i18n `modeIntro.modelBuilder.*` (shared with the intro-card
 * overlay). Example chips `emit("fill-prompt", <prompt>)` to prefill the
 * composer; the "Promote to App Builder" chip routes through the shared
 * `useModeFrameTriggers` bus to ModeFrameModelBuilder.
 */
import { useI18n } from "vue-i18n";

import { useModeFrameTriggers } from "@/composables/useModeFrameTriggers";

const { t } = useI18n();
const { requestOpenPromote } = useModeFrameTriggers();

const emit = defineEmits<{
  "fill-prompt": [text: string];
}>();

function onExample(promptKey: string): void {
  emit("fill-prompt", t(promptKey));
}

function onPromote(): void {
  requestOpenPromote();
}
</script>

<template>
  <div class="mode-empty" data-testid="model-builder-empty-state">
    <header class="mode-empty-head">
      <h2 class="mode-empty-title">{{ t("modeIntro.modelBuilder.title") }}</h2>
      <p class="mode-empty-subtitle">{{ t("modeIntro.modelBuilder.subtitle") }}</p>
    </header>

    <ol class="mode-empty-steps">
      <li class="mode-empty-step">
        <span class="mode-empty-step-num">1</span>
        <span class="mode-empty-step-text">{{ t("modeIntro.modelBuilder.step1") }}</span>
      </li>
      <li class="mode-empty-step">
        <span class="mode-empty-step-num">2</span>
        <span class="mode-empty-step-text">{{ t("modeIntro.modelBuilder.step2") }}</span>
      </li>
      <li class="mode-empty-step">
        <span class="mode-empty-step-num">3</span>
        <span class="mode-empty-step-text">{{ t("modeIntro.modelBuilder.step3") }}</span>
      </li>
    </ol>

    <div class="mode-empty-examples-title">
      {{ t("modeIntro.modelBuilder.emptyExamplesTitle") }}
    </div>
    <div class="mode-empty-chips">
      <button
        type="button"
        class="mode-empty-chip"
        data-testid="model-builder-empty-ex1"
        @click="onExample('modeIntro.modelBuilder.ex1Prompt')"
      >
        {{ t("modeIntro.modelBuilder.ex1Label") }}
      </button>
      <button
        type="button"
        class="mode-empty-chip"
        data-testid="model-builder-empty-ex2"
        @click="onExample('modeIntro.modelBuilder.ex2Prompt')"
      >
        {{ t("modeIntro.modelBuilder.ex2Label") }}
      </button>
      <button
        type="button"
        class="mode-empty-chip"
        data-testid="model-builder-empty-ex3"
        @click="onExample('modeIntro.modelBuilder.ex3Prompt')"
      >
        {{ t("modeIntro.modelBuilder.ex3Label") }}
      </button>
    </div>

    <div class="mode-empty-chips">
      <button
        type="button"
        class="mode-empty-chip mode-empty-chip--primary"
        data-testid="model-builder-empty-promote"
        @click="onPromote"
      >
        {{ t("modeIntro.modelBuilder.chipPromote") }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.mode-empty {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 640px;
  margin: 0 auto;
  padding: 32px 24px;
  color: #e6e6e6;
}

.mode-empty-head {
  text-align: center;
}

.mode-empty-title {
  margin: 0 0 8px;
  font-size: 1.5rem;
  font-weight: 600;
  color: #f5f5f5;
}

.mode-empty-subtitle {
  margin: 0;
  font-size: 0.95rem;
  line-height: 1.5;
  color: #a0a0a8;
}

.mode-empty-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.mode-empty-step {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 12px 16px;
  background: #1c1c22;
  border: 1px solid #2b2b33;
  border-radius: 10px;
}

.mode-empty-step-num {
  flex: 0 0 auto;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: #3a3a45;
  color: #fff;
  font-size: 0.85rem;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.mode-empty-step-text {
  font-size: 0.95rem;
  color: #e6e6e6;
  line-height: 1.4;
}

.mode-empty-examples-title {
  text-align: center;
  font-size: 0.85rem;
  color: #8a8a94;
  margin-top: 4px;
}

.mode-empty-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
}

.mode-empty-chip {
  padding: 9px 18px;
  background: #23232b;
  border: 1px solid #34343f;
  border-radius: 999px;
  color: #d8d8e0;
  font-size: 0.9rem;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.mode-empty-chip:hover {
  background: #2d2d38;
  border-color: #4a4a58;
}

.mode-empty-chip--primary {
  background: linear-gradient(135deg, #6d5efc 0%, #7c6cff 100%);
  border-color: transparent;
  color: #fff;
  font-weight: 600;
}
.mode-empty-chip--primary:hover {
  background: linear-gradient(135deg, #7c6cff 0%, #8b7fff 100%);
  border-color: transparent;
}
</style>
