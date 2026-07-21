<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Sidebar user button — signed-in identity indicator + account menu.
 *
 * Positions (two mount points in AppSidebar):
 *   - EXPANDED footer: occupies the slot the Reboot button used to hold
 *     (Reboot moved INTO this menu).
 *   - COLLAPSED rail: a compact avatar above the expand-arrow, so the
 *     signed-in user is always reachable even when the sidebar is a
 *     60px rail.
 *
 * The popover shows display_name + email and two actions: Restart (was a
 * top-level footer icon) and Sign out (behind a confirm dialog).
 *
 * ── Why the popover is Teleported to <body> ──────────────────────────
 * The sidebar container has `overflow: hidden` (layout.css: `.sidebar`),
 * so an `position:absolute` popover that is a DESCENDANT of the sidebar
 * gets CLIPPED the moment it extends past the sidebar's edge — which is
 * exactly what happened (the username / menu labels were cut off on the
 * right, and badly so in the 60px collapsed rail). `z-index` cannot fix
 * overflow clipping. The only robust fix is to render the popover in a
 * `Teleport to="body"` layer (escaping every ancestor's overflow context)
 * and position it with `position: fixed` computed from the trigger
 * button's `getBoundingClientRect()`. Same approach the app's dialogs use.
 *
 * Renders only when the Okta SSO gate is enabled AND authenticated
 * (`useAuthStore.showUserButton`).
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";

import { redirectToLogout } from "@/api/auth";
import { useAuthStore } from "@/stores/auth";
import { useConfirm } from "@/composables/useConfirm";
import { useReboot } from "@/composables/useReboot";
import { IS_INTERNAL } from "@/edition";

/**
 * Internal-only support contact. Rendered only when `IS_INTERNAL === true`
 * (Rollup dead-code-eliminates the menu item on external/Release builds),
 * so the internal Qualcomm support address never ships in the open-source
 * bundle. The mailto: click hands off to the OS default mail client; if
 * no client is configured the browser silently no-ops — this is a best-effort
 * shortcut, not a critical path.
 */
const SUPPORT_EMAIL = "qai-appbuilder.support@qti.qualcomm.com";

const props = withDefaults(
  defineProps<{ collapsed?: boolean }>(),
  { collapsed: false },
);

const { t } = useI18n();
const auth = useAuthStore();
const { confirm } = useConfirm();
const { requestReboot } = useReboot();

const open = ref(false);
const rootRef = ref<HTMLElement | null>(null);
const btnRef = ref<HTMLButtonElement | null>(null);
const popoverRef = ref<HTMLElement | null>(null);

const POPOVER_WIDTH = 240;
const GAP = 8; // px gap between button and popover
const MARGIN = 8; // min viewport margin

/** Fixed-position style for the teleported popover. */
const popoverStyle = ref<Record<string, string>>({});

const primary = computed<string>(() =>
  auth.user?.display_name?.trim() ||
  auth.user?.username?.trim() ||
  t("auth.signed_in"),
);
const secondary = computed<string>(() => auth.user?.email?.trim() ?? "");

/**
 * Compute the fixed position: anchor the popover ABOVE the button, left
 * edge aligned with the button but clamped into the viewport so it is
 * never clipped by (or spills off) the window on either side.
 */
function positionPopover(): void {
  const btn = btnRef.value;
  if (!btn) return;
  const rect = btn.getBoundingClientRect();
  const vw = window.innerWidth;

  // Horizontal: align left edge with button, clamp to viewport.
  let left = rect.left;
  if (left + POPOVER_WIDTH + MARGIN > vw) {
    left = vw - POPOVER_WIDTH - MARGIN;
  }
  if (left < MARGIN) left = MARGIN;

  // Vertical: bottom of popover sits GAP above the button top → we use
  // `bottom` measured from the viewport bottom so the popover grows
  // upward regardless of its height.
  const bottom = window.innerHeight - rect.top + GAP;

  popoverStyle.value = {
    position: "fixed",
    left: `${Math.round(left)}px`,
    bottom: `${Math.round(bottom)}px`,
    width: `${POPOVER_WIDTH}px`,
  };
}

async function toggle(): Promise<void> {
  open.value = !open.value;
  if (open.value) {
    await nextTick();
    positionPopover();
  }
}

function close(): void {
  open.value = false;
}

function restart(): void {
  close();
  // useReboot() shows its own danger confirm dialog + full-screen overlay.
  void requestReboot();
}

/**
 * Open the OS mail client with a pre-filled To: for the internal support
 * address. Uses `window.location.href` (not `window.open`) so the browser
 * doesn't try to open a blank tab that then immediately closes — Windows
 * hands the mailto: off to the registered handler in place. If no handler
 * is registered, the browser no-ops silently, which is fine (the address
 * is still visible in the /help output as a fallback per the release plan).
 */
function contactSupport(): void {
  close();
  window.location.href = `mailto:${SUPPORT_EMAIL}`;
}

async function signOut(): Promise<void> {
  close();
  const ok = await confirm({
    title: t("auth.sign_out"),
    message: t("auth.sign_out_confirm"),
    confirmText: t("auth.sign_out"),
    cancelText: t("common.cancel"),
    confirmStyle: "danger",
    icon: "\uD83D\uDEAA",
  });
  if (!ok) return;
  redirectToLogout();
}

function onOutside(event: MouseEvent): void {
  if (!open.value) return;
  const target = event.target as Node;
  // Ignore clicks inside the trigger (rootRef) or the teleported popover.
  if (rootRef.value?.contains(target)) return;
  if (popoverRef.value?.contains(target)) return;
  close();
}

function onReposition(): void {
  if (open.value) positionPopover();
}

onMounted(() => {
  document.addEventListener("click", onOutside, true);
  window.addEventListener("resize", onReposition);
  window.addEventListener("scroll", onReposition, true);
});
onBeforeUnmount(() => {
  document.removeEventListener("click", onOutside, true);
  window.removeEventListener("resize", onReposition);
  window.removeEventListener("scroll", onReposition, true);
});
</script>

<template>
  <div
    v-if="auth.showUserButton"
    ref="rootRef"
    class="sidebar-user"
    :class="{ 'sidebar-user--collapsed': props.collapsed }"
  >
    <button
      ref="btnRef"
      type="button"
      class="btn btn-icon sidebar-user-btn"
      :class="{ 'sidebar-user-btn--active': open }"
      :title="secondary ? `${primary} \u00B7 ${secondary}` : primary"
      :aria-label="t('auth.account_menu')"
      :aria-expanded="open"
      aria-haspopup="menu"
      data-testid="sidebar-user-btn"
      @click="toggle"
    >
      <span
        class="sidebar-user-initial"
        aria-hidden="true"
      >{{ auth.initial }}</span>
    </button>

    <!-- Teleported to body to escape the sidebar's overflow:hidden clip. -->
    <Teleport to="body">
      <div
        v-if="open"
        ref="popoverRef"
        class="sidebar-user-popover"
        :style="popoverStyle"
        role="menu"
        data-testid="sidebar-user-popover"
      >
        <div class="sidebar-user-info">
          <div class="sidebar-user-name">{{ primary }}</div>
          <div
            v-if="secondary !== ''"
            class="sidebar-user-email"
            :title="secondary"
          >{{ secondary }}</div>
        </div>
        <div class="sidebar-user-divider"></div>

        <!-- Restart (moved here from the footer icon row) -->
        <button
          type="button"
          class="sidebar-user-item"
          role="menuitem"
          data-testid="sidebar-user-restart"
          @click="restart"
        >
          <svg
            class="sidebar-user-item-icon"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            aria-hidden="true"
          >
            <polyline points="23 4 23 10 17 10" />
            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
          </svg>
          <span class="sidebar-user-item-label">{{ t("sidebar.reboot") }}</span>
        </button>

        <!-- Contact Support (internal-only) — opens the OS mail client with
             the Qualcomm internal support address pre-filled. IS_INTERNAL
             is a build-time constant; the entire button is DCE'd out of the
             external/Release bundle, so the internal email never ships. -->
        <button
          v-if="IS_INTERNAL"
          type="button"
          class="sidebar-user-item"
          role="menuitem"
          data-testid="sidebar-user-contact-support"
          @click="contactSupport"
        >
          <svg
            class="sidebar-user-item-icon"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            aria-hidden="true"
          >
            <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" />
            <polyline points="22,6 12,13 2,6" />
          </svg>
          <span class="sidebar-user-item-label">{{ t("auth.contact_support") }}</span>
        </button>

        <!-- Sign out (behind a confirm dialog) -->
        <button
          type="button"
          class="sidebar-user-item sidebar-user-item--danger"
          role="menuitem"
          data-testid="sidebar-user-signout"
          @click="signOut"
        >
          <svg
            class="sidebar-user-item-icon"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            aria-hidden="true"
          >
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
            <polyline points="16 17 21 12 16 7" />
            <line x1="21" y1="12" x2="9" y2="12" />
          </svg>
          <span class="sidebar-user-item-label">{{ t("auth.sign_out") }}</span>
        </button>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
/* Trigger wrapper — no positioning context needed anymore (popover is
   teleported + position:fixed). */
.sidebar-user {
  display: inline-flex;
}

.sidebar-user-btn {
  padding: 0;
}

.sidebar-user-initial {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  font-size: 11px;
  font-weight: 600;
  line-height: 1;
  color: var(--text-primary);
  transition: background 0.15s ease, border-color 0.15s ease;
}

.sidebar-user-btn:hover .sidebar-user-initial {
  border-color: var(--accent);
}

.sidebar-user-btn--active {
  background: var(--bg-hover);
}
.sidebar-user-btn--active .sidebar-user-initial {
  background: var(--accent);
  border-color: transparent;
  color: #fff;
}
</style>

<!-- Popover styles are NOT scoped: the popover is teleported to <body>,
     outside this component's DOM subtree, so scoped `data-v-*` attributes
     would not match. A dedicated class namespace keeps them isolated. -->
<style>
.sidebar-user-popover {
  /* position/left/bottom/width are set inline via :style */
  background: var(--glass-bg);
  backdrop-filter: blur(var(--glass-blur, 12px));
  -webkit-backdrop-filter: blur(var(--glass-blur, 12px));
  border: 1px solid var(--border);
  border-radius: var(--radius, 12px);
  box-shadow: var(--shadow-lg);
  padding: 4px 0;
  z-index: 9600; /* above app chrome, below reboot overlay (9999) */
}

.sidebar-user-popover .sidebar-user-info {
  padding: 10px 12px;
}
.sidebar-user-popover .sidebar-user-name {
  font-size: var(--text-base, 13px);
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sidebar-user-popover .sidebar-user-email {
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, var(--text-secondary));
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sidebar-user-popover .sidebar-user-divider {
  height: 1px;
  background: var(--border);
  margin: 4px 0;
}

.sidebar-user-popover .sidebar-user-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 12px;
  border: 0;
  background: transparent;
  color: var(--text-primary);
  font-size: var(--text-base, 13px);
  cursor: pointer;
  text-align: left;
  font-family: inherit;
  transition: background 0.15s ease, color 0.15s ease;
}
.sidebar-user-popover .sidebar-user-item:hover {
  background: var(--bg-hover);
}
.sidebar-user-popover .sidebar-user-item--danger:hover {
  color: var(--danger, #ef4444);
}

.sidebar-user-popover .sidebar-user-item-icon {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
}
.sidebar-user-popover .sidebar-user-item-label {
  flex: 1;
}
</style>
