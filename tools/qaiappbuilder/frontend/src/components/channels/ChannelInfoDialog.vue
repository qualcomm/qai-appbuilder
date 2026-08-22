<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChannelInfoDialog — shared modal shell for the WeChat / Feishu info dialogs
 * and the generic channels usage guide (V1 `wechat-help-modal` chrome).
 *
 * Provides the teleported overlay, header (icon + title + subtitle + close),
 * scrollable body slot, and footer "Got it" button. Each caller fills the
 * body slot with its own sections; this component owns only the chrome so the
 * three dialogs do not duplicate ~40 lines of modal markup each (cohesion:
 * one source of truth for the dialog frame).
 */
import { ref, toRef } from "vue";
import { useI18n } from "vue-i18n";
import { useFocusTrap } from "@/composables/useFocusTrap";

const props = defineProps<{
  open: boolean;
  icon: string;
  title: string;
  subtitle: string;
}>();

const emit = defineEmits<{ close: [] }>();

const { t } = useI18n();

// V1 parity (utils/focus-trap.js): every modal that uses this shell —
// WechatInfoDialog / FeishuInfoDialog / ChannelsGuideDialog — gets Tab
// focus cycling + opener-focus restore "for free" via the shared shell.
// Esc closes via the existing close emit.
const dialogEl = ref<HTMLElement | null>(null);
useFocusTrap(dialogEl, {
  active: toRef(props, "open"),
  onEscape: () => emit("close"),
  focusFirst: true,
});
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="channel-info-overlay"
      @click.self="emit('close')"
    >
      <div
        ref="dialogEl"
        class="channel-info-modal"
        role="dialog"
        aria-modal="true"
      >
        <div class="channel-info-header">
          <div class="channel-info-header__icon">
            <!-- V1 parity: WeChat uses an inline SVG logo, the other dialogs
                 use an emoji string. The `icon` slot lets a caller inject the
                 SVG; otherwise the `icon` prop string is rendered. -->
            <slot name="icon">
              {{ icon }}
            </slot>
          </div>
          <div class="channel-info-header__text">
            <div class="channel-info-title">
              {{ title }}
            </div>
            <div class="channel-info-subtitle">
              {{ subtitle }}
            </div>
          </div>
          <button
            class="channel-info-close"
            type="button"
            :title="t('common.close')"
            @click="emit('close')"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2.5"
              stroke-linecap="round"
            >
              <line
                x1="18"
                y1="6"
                x2="6"
                y2="18"
              />
              <line
                x1="6"
                y1="6"
                x2="18"
                y2="18"
              />
            </svg>
          </button>
        </div>

        <div class="channel-info-body">
          <slot />
        </div>

        <div class="channel-info-footer">
          <button
            class="btn btn-primary btn-sm"
            type="button"
            @click="emit('close')"
          >
            {{ t("common.gotIt") }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* Mirrors V1 `.wechat-help-*` chrome (css/channels.css:179-339). The three
   channel info dialogs (WeChat / Feishu / usage guide) all share this shell,
   so the dialog frame + the body section-title / notice-callout styling live
   here once (via :slotted) rather than being duplicated in every caller. */
.channel-info-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay-bg);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4);
  z-index: 2000;
  animation: channel-info-overlay-in 0.15s ease;
}

@keyframes channel-info-overlay-in {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}

.channel-info-modal {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  width: 100%;
  max-width: 520px;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-lg);
  overflow: hidden;
  animation: channel-info-modal-in 0.2s ease;
}

@keyframes channel-info-modal-in {
  from {
    opacity: 0;
    transform: translateY(-12px) scale(0.97);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.channel-info-header {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.channel-info-header__icon {
  font-size: var(--text-xl);
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-tertiary);
  border-radius: var(--radius-sm);
  flex-shrink: 0;
}

.channel-info-header__text {
  flex: 1;
}

.channel-info-title {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--text-primary);
}

.channel-info-subtitle {
  font-size: var(--text-sm);
  color: var(--text-muted);
  margin-top: 2px;
}

.channel-info-close {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  background: none;
  border: none;
  border-radius: 6px;
  color: var(--text-muted);
  cursor: pointer;
  transition: background var(--transition), color var(--transition);
  flex-shrink: 0;
}

.channel-info-close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.channel-info-body {
  flex: 1;
  padding: var(--space-4) var(--space-5);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.channel-info-footer {
  display: flex;
  justify-content: flex-end;
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}
</style>
