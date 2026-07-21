<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CodeEmptyState — the mode-specific welcome screen shown in the「编程 /
 * Claude Code」chat when there are no messages yet. Replaces the generic
 * welcome (chat.welcomeTitle + generic starter chips) with a 3-step guide
 * + two primary CTAs (Pick persona / Upload code) that route via the
 * shared `useModeFrameTriggers` bump-token bus to ModeFrameCoding.
 *
 * Design parity: mirrors the visual language of AppBuilderEmptyState /
 * GomasterEmptyState / ProEmptyState so the mode welcome screens read as
 * one family. Copy is shared with the intro-card overlay via i18n keys
 * (`modeIntro.code.*`) — single source of truth for the mode's onboarding
 * language.
 */
import { useI18n } from "vue-i18n";

import { useModeFrameTriggers } from "@/composables/useModeFrameTriggers";

const { t } = useI18n();
const { requestOpenCodePersona, requestOpenCodeContext } = useModeFrameTriggers();

function onPickPersona(): void {
  requestOpenCodePersona();
}

function onUploadContext(): void {
  requestOpenCodeContext();
}
</script>

<template>
  <div class="code-empty" data-testid="code-empty-state">
    <header class="code-empty-head">
      <h2 class="code-empty-title">{{ t("modeIntro.code.title") }}</h2>
      <p class="code-empty-subtitle">{{ t("modeIntro.code.subtitle") }}</p>
    </header>

    <ol class="code-empty-steps">
      <li class="code-empty-step">
        <span class="code-empty-step-num">1</span>
        <span class="code-empty-step-text">{{ t("modeIntro.code.step1") }}</span>
      </li>
      <li class="code-empty-step">
        <span class="code-empty-step-num">2</span>
        <span class="code-empty-step-text">{{ t("modeIntro.code.step2") }}</span>
      </li>
      <li class="code-empty-step">
        <span class="code-empty-step-num">3</span>
        <span class="code-empty-step-text">{{ t("modeIntro.code.step3") }}</span>
      </li>
    </ol>

    <div class="code-empty-chips">
      <button
        type="button"
        class="code-empty-chip code-empty-chip--primary"
        data-testid="code-empty-open-persona"
        @click="onPickPersona"
      >
        {{ t("modeIntro.code.chipPersona") }}
      </button>
      <button
        type="button"
        class="code-empty-chip"
        data-testid="code-empty-open-context"
        @click="onUploadContext"
      >
        {{ t("modeIntro.code.chipContext") }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.code-empty {
  display: flex;
  flex-direction: column;
  gap: 20px;
  max-width: 640px;
  margin: 0 auto;
  padding: 32px 24px;
  color: #e6e6e6;
}

.code-empty-head {
  text-align: center;
}

.code-empty-title {
  margin: 0 0 8px;
  font-size: 1.5rem;
  font-weight: 600;
  color: #f5f5f5;
}

.code-empty-subtitle {
  margin: 0;
  font-size: 0.95rem;
  line-height: 1.5;
  color: #a0a0a8;
}

.code-empty-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.code-empty-step {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 12px 16px;
  background: #1c1c22;
  border: 1px solid #2b2b33;
  border-radius: 10px;
}

.code-empty-step-num {
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

.code-empty-step-text {
  font-size: 0.95rem;
  color: #e6e6e6;
  line-height: 1.4;
}

.code-empty-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
  margin-top: 4px;
}

.code-empty-chip {
  padding: 9px 18px;
  background: #23232b;
  border: 1px solid #34343f;
  border-radius: 999px;
  color: #d8d8e0;
  font-size: 0.9rem;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.code-empty-chip:hover {
  background: #2d2d38;
  border-color: #4a4a58;
}

.code-empty-chip--primary {
  background: linear-gradient(135deg, #6d5efc 0%, #7c6cff 100%);
  border-color: transparent;
  color: #fff;
  font-weight: 600;
}
.code-empty-chip--primary:hover {
  background: linear-gradient(135deg, #7c6cff 0%, #8b7fff 100%);
  border-color: transparent;
}
</style>
