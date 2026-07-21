<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * LoginPrompt — mandatory, non-dismissable Okta sign-in invitation.
 *
 * Shown (via `auth.showLoginPrompt`) when the SSO gate is enabled and the
 * user is not authenticated. Replaces the old jarring "hard-redirect the
 * whole page to Okta on load" behaviour: the SPA renders normally behind
 * a blurred backdrop, and this modal explains that sign-in is required
 * and offers a single "Sign in" button that navigates to the Okta flow.
 *
 * Non-dismissable by design (per product decision): no close button, no
 * backdrop-click handler, no Esc handler — because every business API is
 * 401 until the user signs in, so there is nothing to interact with
 * behind it anyway.
 *
 * Styling reuses the shared dialog tokens (`--overlay-bg`, `--bg-secondary`,
 * `--border`, `--radius-lg`, `--shadow-lg`, `--accent`) so it adapts to
 * light / dark themes with zero extra work — same look-and-feel as
 * RenameDialog / ConfirmDialog.
 */
import { computed, ref, watch, nextTick } from "vue";
import { useI18n } from "vue-i18n";

import { redirectToLogin } from "@/api/auth";
import { useAuthStore } from "@/stores/auth";

const { t } = useI18n();
const auth = useAuthStore();

const visible = computed<boolean>(() => auth.showLoginPrompt);

const signingIn = ref(false);
const signInBtn = ref<HTMLButtonElement | null>(null);

function signIn(): void {
  // Full-document navigation to the server login endpoint → Okta. Set a
  // local flag so the button shows a "redirecting…" state during the
  // brief moment before the browser leaves the page.
  signingIn.value = true;
  redirectToLogin();
}

// Autofocus the primary button when the modal appears (accessibility +
// lets the user just press Enter/Space to proceed).
watch(visible, async (v) => {
  if (v) {
    await nextTick();
    signInBtn.value?.focus();
  }
});
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="login-prompt-overlay"
      role="presentation"
    >
      <div
        class="login-prompt-card"
        role="dialog"
        aria-modal="true"
        :aria-label="t('auth.prompt_title')"
        data-testid="login-prompt"
      >
        <!-- Brand logo (inline SVG, matches the sidebar glyph gradient) -->
        <div class="login-prompt-logo" aria-hidden="true">
          <svg viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
            <defs>
              <linearGradient id="loginLogoGrad" x1="0" y1="0" x2="48" y2="48" gradientUnits="userSpaceOnUse">
                <stop stop-color="#7c6cff" />
                <stop offset="1" stop-color="#60a5fa" />
              </linearGradient>
            </defs>
            <rect x="4" y="4" width="40" height="40" rx="10" fill="url(#loginLogoGrad)" opacity="0.16" />
            <rect x="17" y="17" width="14" height="14" rx="3" stroke="url(#loginLogoGrad)" stroke-width="2" />
            <circle cx="24" cy="9" r="2.4" fill="url(#loginLogoGrad)" />
            <circle cx="24" cy="39" r="2.4" fill="url(#loginLogoGrad)" />
            <circle cx="9" cy="24" r="2.4" fill="url(#loginLogoGrad)" />
            <circle cx="39" cy="24" r="2.4" fill="url(#loginLogoGrad)" />
            <path d="M24 11.4V17M24 31v5.6M11.4 24H17M31 24h5.6" stroke="url(#loginLogoGrad)" stroke-width="2" stroke-linecap="round" />
          </svg>
        </div>

        <h2 class="login-prompt-title">{{ t("auth.prompt_title") }}</h2>
        <p class="login-prompt-message">{{ t("auth.prompt_message") }}</p>

        <button
          ref="signInBtn"
          type="button"
          class="btn btn-primary login-prompt-btn"
          :disabled="signingIn"
          data-testid="login-prompt-signin"
          @click="signIn"
        >
          <span v-if="!signingIn">{{ t("auth.sign_in") }}</span>
          <span v-else>{{ t("auth.redirecting") }}</span>
        </button>

        <p class="login-prompt-hint">{{ t("auth.prompt_hint") }}</p>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.login-prompt-overlay {
  position: fixed;
  inset: 0;
  z-index: 9500; /* above app chrome, below reboot overlay (which is 9999) */
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--overlay-bg);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  padding: var(--space-4);
}

.login-prompt-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  width: 380px;
  max-width: 92vw;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  padding: var(--space-8) var(--space-6) var(--space-6);
  animation: login-prompt-in 0.22s ease-out;
}

@keyframes login-prompt-in {
  from {
    opacity: 0;
    transform: translateY(-10px) scale(0.97);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

.login-prompt-logo {
  width: 64px;
  height: 64px;
  margin-bottom: var(--space-4);
}
.login-prompt-logo svg {
  width: 100%;
  height: 100%;
  display: block;
}

.login-prompt-title {
  margin: 0 0 var(--space-2);
  font-size: var(--text-xl, 20px);
  font-weight: var(--weight-semibold, 600);
  color: var(--text-primary);
}

.login-prompt-message {
  margin: 0 0 var(--space-5);
  font-size: var(--text-md, 14px);
  line-height: 1.5;
  color: var(--text-secondary);
}

.login-prompt-btn {
  width: 100%;
  height: 42px;
  font-size: var(--text-md, 14px);
  font-weight: var(--weight-semibold, 600);
}

.login-prompt-hint {
  margin: var(--space-4) 0 0;
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, var(--text-secondary));
  line-height: 1.4;
}
</style>
