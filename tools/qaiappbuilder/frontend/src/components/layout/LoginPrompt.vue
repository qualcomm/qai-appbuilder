<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
import { computed, ref, watch, nextTick } from "vue";
import { useI18n } from "vue-i18n";

import { silentLogin, popupLogin, redirectToLogin } from "@/api/auth";
import { releaseAuthGate } from "@/api/http";
import { useAuthStore } from "@/stores/auth";

const { t } = useI18n();
const auth = useAuthStore();

const visible = computed<boolean>(() => auth.showLoginPrompt);

const signingIn = ref(false);
const popupBlocked = ref(false);
const signInBtn = ref<HTMLButtonElement | null>(null);

async function signIn(): Promise<void> {
  signingIn.value = true;
  popupBlocked.value = false;
  // Mark the flow as in-flight so any request that 401s while Okta is being
  // contacted does NOT re-raise this prompt on top of the running sign-in.
  // Without it a second popup opened mid-flow and the user logged in twice.
  auth.beginLogin();
  try {
    // Phase 1: try silent re-auth (hidden iframe, no user-visible window).
    // Succeeds instantly when the IdP session is still valid.
    await silentLogin();
    await auth.refresh();
    releaseAuthGate();
    signingIn.value = false;
    auth.endLogin();
    return;
  } catch {
    // Silent failed (IdP session expired or CSP blocked) — fall through to popup.
  }

  // Phase 2: open visible popup for interactive Okta login.
  try {
    await popupLogin();
    await auth.refresh();
    releaseAuthGate();
  } catch (err: unknown) {
    if (err instanceof Error && err.message === "popup_blocked") {
      popupBlocked.value = true;
    }
    // popup_closed is expected (user dismissed) — no action needed.
  } finally {
    signingIn.value = false;
    // Release the window on EVERY exit path: a successful ``refresh`` already
    // cleared it, but a cancelled / blocked popup must not keep the prompt
    // suppressed — otherwise a genuinely expired session stops asking.
    auth.endLogin();
  }
}

/** Fallback: redirect the main window to Okta when popup is blocked. */
function signInRedirect(): void {
  redirectToLogin();
}

// Autofocus the primary button when the modal appears
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

        <!-- Register link. Opens Qualcomm's public account-signup page in a
             NEW tab so the login prompt (and any app state behind it) stays
             intact while the user completes registration. After creating
             their Qualcomm ID the user returns to this tab and clicks
             "Sign in" — the standard OIDC flow then picks up the freshly
             minted account.

             `rel="noopener noreferrer"` is required with `target="_blank"`
             (blocks `window.opener` reverse-tab-nabbing and strips the
             Referer header on the outbound navigation). Ships as HTML `href`
             so keyboard users can Tab to it and Enter to activate. -->
        <a
          class="login-prompt-register"
          href="https://myaccount.qualcomm.com/signup"
          target="_blank"
          rel="noopener noreferrer"
          data-testid="login-prompt-register"
        >{{ t("auth.register_prompt") }}</a>

        <!-- Popup blocked fallback -->
        <div v-if="popupBlocked" class="login-prompt-blocked">
          <p class="login-prompt-blocked-msg">
            Popup window was blocked by your browser.
          </p>
          <div class="login-prompt-blocked-actions">
            <button
              type="button"
              class="btn btn-primary login-prompt-btn"
              @click="signIn"
            >
              Retry Popup
            </button>
            <button
              type="button"
              class="btn login-prompt-btn login-prompt-btn--secondary"
              @click="signInRedirect"
            >
              Sign in via redirect
            </button>
          </div>
        </div>

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

/* Register link — secondary affordance under the primary Sign-in button.
 * Text-only, `--text-secondary` normally, `--accent` on hover/focus to
 * signal it's actionable. Ships as an `<a>` (not a `<button>`) because it
 * truly IS a navigation to a new page — the assistive-tech affordance and
 * the browser status-bar URL preview are both real. */
.login-prompt-register {
  display: block;
  margin: var(--space-3) 0 0;
  text-align: center;
  font-size: var(--text-sm, 13px);
  color: var(--text-secondary);
  text-decoration: none;
  transition: color 0.15s ease;
}
.login-prompt-register:hover,
.login-prompt-register:focus-visible {
  color: var(--accent);
  text-decoration: underline;
}

.login-prompt-blocked {
  margin-top: var(--space-3);
  text-align: center;
}

.login-prompt-blocked-msg {
  margin: 0 0 var(--space-3);
  font-size: var(--text-sm, 13px);
  color: var(--text-warning, #d97706);
}

.login-prompt-blocked-actions {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.login-prompt-btn--secondary {
  background: var(--bg-secondary, rgba(128, 128, 128, 0.1));
  color: var(--text-primary);
  border: 1px solid var(--border, rgba(128, 128, 128, 0.2));
}

.login-prompt-btn--secondary:hover {
  background: var(--bg-hover, rgba(128, 128, 128, 0.15));
}

.login-prompt-hint {
  margin: var(--space-4) 0 0;
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, var(--text-secondary));
  line-height: 1.4;
}
</style>
