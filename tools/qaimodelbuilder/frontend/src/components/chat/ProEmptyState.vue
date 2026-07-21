<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ProEmptyState — the mode-specific welcome screen shown in the「增强 / Pro」
 * chat when there are no messages yet. Replaces the generic welcome
 * (chat.welcomeTitle + generic starter chips) with a 3-step guide + two
 * primary CTAs (Open settings / Connect GPU Agent) that route via the
 * shared `useModeFrameTriggers` bump-token bus to ModeFramePro.
 *
 * Design parity: mirrors the visual language of AppBuilderEmptyState /
 * GomasterEmptyState so the three welcome screens read as one family.
 * Copy is shared with the intro-card overlay via i18n keys
 * (`modeIntro.pro.*`) — single source of truth for the mode's onboarding
 * language.
 *
 * Access-gate parity with ModeFramePro (2026-07-19): the "Connect GPU
 * Agent" chip mirrors the connect button in `ModeFramePro.vue:290-346`.
 * When the user lacks MB Pro authorization (LDAP not a member of
 * `ModelBuilderProUsers`, OR LDAP service unreachable) the chip is
 * disabled and hovering shows the same tooltip as the toolbar — with
 * "Apply to join" + "Refresh access" actions. Rationale: users otherwise
 * click Connect, get a downstream HTTP 403 in a small error text, and
 * have no discoverable path to fix it. The "Open settings" chip stays
 * enabled regardless of access — users may want to inspect / adjust
 * settings (e.g. SSL verify) before or independently of connecting.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";

import { useAuthStore } from "@/stores/auth";
import { useMbProAccessGate } from "@/composables/useMbProAccessGate";
import { useModeFrameTriggers } from "@/composables/useModeFrameTriggers";

const { t } = useI18n();
const { requestOpenProSettings, requestOpenProConnect } = useModeFrameTriggers();

// ── MB Pro access-gate (parity with ModeFramePro) ────────────────────────────
// Authorization state (isMbProAuthorized / mbProAccessCheckFailed) lives in
// useAuthStore — server-driven, single source of truth. The tooltip visibility
// state machine + Apply/Refresh actions are shared with the toolbar chip via
// `useMbProAccessGate` so both surfaces stay in lock-step (previously each
// reimplemented the debounce with a 120ms vs 150ms drift).
const authStore = useAuthStore();
const isMbProAuthorized = computed(() => authStore.isMbProAuthorized);
const mbProAccessCheckFailed = computed(() => authStore.mbProAccessCheckFailed);

const {
  isRefreshingAccess,
  accessTooltipVisible,
  showAccessTooltip,
  hideAccessTooltip,
  onRefreshAccess,
  onApplyToJoin,
} = useMbProAccessGate();

function onOpenSettings(): void {
  requestOpenProSettings();
}

function onConnect(): void {
  // Guard: only the authorized path bumps the connect trigger. When
  // unauthorized the button is disabled at the DOM level (see template),
  // this is belt-and-braces so a synthetic click cannot bypass the gate.
  if (!isMbProAuthorized.value) return;
  requestOpenProConnect();
}
</script>

<template>
  <div class="pro-empty" data-testid="pro-empty-state">
    <header class="pro-empty-head">
      <h2 class="pro-empty-title">{{ t("modeIntro.pro.title") }}</h2>
      <p class="pro-empty-subtitle">{{ t("modeIntro.pro.subtitle") }}</p>
    </header>

    <ol class="pro-empty-steps">
      <li class="pro-empty-step">
        <span class="pro-empty-step-num">1</span>
        <span class="pro-empty-step-text">{{ t("modeIntro.pro.step1") }}</span>
      </li>
      <li class="pro-empty-step">
        <span class="pro-empty-step-num">2</span>
        <span class="pro-empty-step-text">{{ t("modeIntro.pro.step2") }}</span>
      </li>
      <li class="pro-empty-step">
        <span class="pro-empty-step-num">3</span>
        <span class="pro-empty-step-text">{{ t("modeIntro.pro.step3") }}</span>
      </li>
    </ol>

    <div class="pro-empty-chips">
      <button
        type="button"
        class="pro-empty-chip pro-empty-chip--primary"
        data-testid="pro-empty-open-settings"
        @click="onOpenSettings"
      >
        {{ t("modeIntro.pro.chipSettings") }}
      </button>

      <!-- Access-gated Connect chip. Parity with ModeFramePro.vue:290-346:
           when unauthorized the button is disabled + a tooltip explains why
           (LDAP service unreachable vs "not a member") and offers Apply /
           Refresh actions. The wrapper span carries the hover handlers so
           they fire even on a disabled button (which browsers exempt from
           some pointer events). -->
      <template v-if="!isMbProAuthorized">
        <span
          class="pro-empty-chip-wrapper"
          @mouseenter="showAccessTooltip"
          @mouseleave="hideAccessTooltip"
        >
          <button
            type="button"
            class="pro-empty-chip"
            data-testid="pro-empty-open-connect"
            disabled
          >
            {{ t("modeIntro.pro.chipConnect") }}
          </button>
          <div
            v-show="accessTooltipVisible"
            class="pro-empty-access-tooltip"
            data-testid="pro-empty-access-tooltip"
            @mouseenter="showAccessTooltip"
            @mouseleave="hideAccessTooltip"
          >
            <template v-if="mbProAccessCheckFailed">
              <p class="pro-empty-access-tooltip-msg">
                {{ t("index.proAccessLdapError") }}
              </p>
            </template>
            <template v-else>
              <p class="pro-empty-access-tooltip-title">
                {{ t("index.proAccessDeniedTitle") }}
              </p>
              <p class="pro-empty-access-tooltip-msg">
                {{ t("index.proAccessDeniedBody") }}
              </p>
              <div class="pro-empty-access-tooltip-actions">
                <button
                  type="button"
                  class="pro-empty-chip pro-empty-chip--sm"
                  @click="onApplyToJoin"
                >{{ t("index.proAccessApply") }}</button>
                <button
                  type="button"
                  class="pro-empty-chip pro-empty-chip--sm"
                  :disabled="isRefreshingAccess"
                  @click="onRefreshAccess"
                >{{ isRefreshingAccess ? t("index.proAccessRefreshing") : t("index.proAccessRefresh") }}</button>
              </div>
            </template>
          </div>
        </span>
      </template>
      <template v-else>
        <button
          type="button"
          class="pro-empty-chip"
          data-testid="pro-empty-open-connect"
          @click="onConnect"
        >
          {{ t("modeIntro.pro.chipConnect") }}
        </button>
      </template>
    </div>
  </div>
</template>

<style scoped>
/*
 * Visual parity with AppBuilderEmptyState / GomasterEmptyState so the three
 * mode welcome screens read as one family. Uses hard-coded colours that match
 * those two components rather than CSS tokens; the app's welcome-screen
 * design has already committed to this palette (see AppBuilderEmptyState).
 */
.pro-empty {
  display: flex;
  flex-direction: column;
  gap: 20px;
  max-width: 640px;
  margin: 0 auto;
  padding: 32px 24px;
  color: #e6e6e6;
}

.pro-empty-head {
  text-align: center;
}

.pro-empty-title {
  margin: 0 0 8px;
  font-size: 1.5rem;
  font-weight: 600;
  color: #f5f5f5;
}

.pro-empty-subtitle {
  margin: 0;
  font-size: 0.95rem;
  line-height: 1.5;
  color: #a0a0a8;
}

.pro-empty-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.pro-empty-step {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 12px 16px;
  background: #1c1c22;
  border: 1px solid #2b2b33;
  border-radius: 10px;
}

.pro-empty-step-num {
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

.pro-empty-step-text {
  font-size: 0.95rem;
  color: #e6e6e6;
  line-height: 1.4;
}

.pro-empty-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
  margin-top: 4px;
}

.pro-empty-chip {
  padding: 9px 18px;
  background: #23232b;
  border: 1px solid #34343f;
  border-radius: 999px;
  color: #d8d8e0;
  font-size: 0.9rem;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.pro-empty-chip:hover {
  background: #2d2d38;
  border-color: #4a4a58;
}

.pro-empty-chip--primary {
  background: linear-gradient(135deg, #6d5efc 0%, #7c6cff 100%);
  border-color: transparent;
  color: #fff;
  font-weight: 600;
}
.pro-empty-chip--primary:hover {
  background: linear-gradient(135deg, #7c6cff 0%, #8b7fff 100%);
  border-color: transparent;
}

/* ── Access-gate: disabled Connect chip + tooltip ───────────────────────────
 * Visual language mirrors ModeFramePro.vue:451-482 so the two access-denied
 * surfaces read identically. Position differs from ModeFramePro (which pops
 * upward from the toolbar): here the chips are mid-page so the tooltip pops
 * downward. Colours: hard-coded to match the surrounding empty-state palette
 * (see .pro-empty-step above) — the component has already committed to this
 * palette rather than CSS tokens.
 */
.pro-empty-chip:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.pro-empty-chip:disabled:hover {
  background: #23232b;
  border-color: #34343f;
}
.pro-empty-chip-wrapper {
  position: relative;
  display: inline-block;
}
.pro-empty-access-tooltip {
  position: absolute;
  top: calc(100% + 8px);
  left: 50%;
  transform: translateX(-50%);
  background: #1e1e2e;
  border: 1px solid #333;
  border-radius: 8px;
  padding: 12px 14px;
  width: 260px;
  z-index: 999;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
  white-space: normal;
  text-align: left;
}
.pro-empty-access-tooltip-title {
  font-weight: 600;
  margin: 0 0 4px;
  font-size: 0.875rem;
  color: #f5f5f5;
}
.pro-empty-access-tooltip-msg {
  font-size: 0.8125rem;
  color: #a0a0a8;
  margin: 0 0 10px;
  line-height: 1.4;
}
.pro-empty-access-tooltip-actions {
  display: flex;
  gap: 8px;
}
.pro-empty-chip--sm {
  padding: 5px 12px;
  font-size: 0.8125rem;
}
</style>
