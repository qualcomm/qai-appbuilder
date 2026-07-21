<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Top-level layout shell.
 *
 * S5 PR-050: minimal stub.
 * S5 PR-052: real chrome — header / sidebar / main / toast host /
 * command palette. The view-level `RouterView` lives inside
 * `AppMain.vue` (which owns the Suspense + ErrorBoundary).
 *
 * Guards are installed here (rather than in `main.ts`, which is
 * frozen by PR-050) because we have access to both the router and
 * the i18n instance via composition-API hooks at this point in the
 * mount lifecycle.
 */
import { onMounted, onBeforeUnmount, watch } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useUiStore } from "@/stores/ui";
import { useCommandPaletteStore } from "@/stores/commandPalette";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useConversationsStore } from "@/stores/conversations";
import { useAuthStore } from "@/stores/auth";
import { useServiceStore } from "@/stores/service";
import { useCloudModelPermissionsStore } from "@/stores/cloudModelPermissions";
import { useTheme } from "@/composables/useTheme";
import { useFontSize } from "@/composables/useFontSize";
import { useCommandPalette } from "@/composables/useCommandPalette";
import { useToast } from "@/composables/useToast";
import { useKeymap, type KeymapBinding } from "@/composables/keymap";
import { isDesktopShell } from "@/utils/platform";
import { useReboot } from "@/composables/useReboot";
import { usePermissionDialog } from "@/composables/security/usePermissionDialog";
import { connectGlobalEvents } from "@/api/globalEvents";
import { apiBaseUrl } from "@/api/base";
import { setAuthRequiredHandler } from "@/api/http";
import { installGuards } from "@/router";
import { registerCloudModelSettingsNavigator } from "@/composables/useCloudModelStatus";
import AppHeader from "@/components/layout/AppHeader.vue";
import AppSidebar from "@/components/layout/AppSidebar.vue";
import AppMain from "@/components/layout/AppMain.vue";
import AppToastHost from "@/components/layout/AppToastHost.vue";
import ConfirmDialog from "@/components/layout/ConfirmDialog.vue";
import AppCommandPalette from "@/components/layout/AppCommandPalette.vue";
import RebootOverlay from "@/components/layout/RebootOverlay.vue";
import SecurityDialog from "@/components/security/SecurityDialog.vue";
import LoginPrompt from "@/components/layout/LoginPrompt.vue";

const ui = useUiStore();
const router = useRouter();
const i18n = useI18n();
const palette = useCommandPaletteStore();
const chatTabs = useChatTabsStore();
const conversations = useConversationsStore();
// Okta SSO snapshot — hydrated once at mount, then kept fresh by the
// router beforeEach guard on every navigation. Exposed on the sidebar
// user button (SidebarUserButton.vue reads the same store).
const auth = useAuthStore();
// Edition flag (internal vs external) — fetched once at mount and consumed
// by the guided "missing cloud API key" flow (useCloudModelStatus.openApiKeyFlow).
const service = useServiceStore();
// Cloud-model permission snapshot — populated once at mount from the
// backend's lifespan-scanned per-model permissions. Fail-open: if the fetch
// fails the store stays empty and every model shows in the dropdown (matches
// the never-preset-unavailable UX principle).
const cloudModelPermissions = useCloudModelPermissionsStore();
// Toast host is mounted globally (AppToastHost); use it for the
// welcome-back message after a successful sign-in.
const toast = useToast();
// Reboot transition (V1 useChat.js:2894-2900): the server pushes a `reboot`
// SSE event on `/api/events` when a restart is imminent. We surface it via the
// shared reboot controller so the full-screen overlay + health-poll +
// auto-refresh fire even when the restart is initiated elsewhere (e.g. another
// tab, or the backend's own supervisor). The sidebar button / chat `/reboot`
// command also enter this same transition locally.
const { beginReboot } = useReboot();
// File-access authorization dialog (V1 parity): App.vue owns the single
// `/api/events` SSE connection, so it forwards `permission_request` frames
// into the shared permission-dialog queue and re-pulls未决项 on (re)connect.
const permissionDialog = usePermissionDialog();
let disconnectGlobalEvents: (() => void) | null = null;
/** Guards against concurrent health probes on rapid SSE error bursts. */
let _isRebootProbing = false;

// Keep the vue-i18n active locale in sync with the UI store. The
// language switcher only mutates `ui.locale`; without this bridge the
// rendered locale never changes (so every `t(...)` stays on the initial
// language). `immediate` applies the persisted locale on first load.
watch(
  () => ui.locale,
  (loc) => {
    if (i18n.locale.value !== loc) {
      i18n.locale.value = loc as typeof i18n.locale.value;
    }
  },
  { immediate: true },
);

// --- Sandbox notification state ---
// Removed 2026-07-01 along with the Windows ACL / sandbox UI cleanup. The
// underlying `SandboxNotification` component was deleted and `showSandboxNotification`
// was never set to true anywhere in the codebase (dead code).

// Install router guards (document.title) and theme/system-pref bridge.
// The duck-typed `I18nLike` shape — `{ t(key): string }` — matches
// both the global I18n instance and the local Composer returned by
// `useI18n()`, so we can pass either without a cast.
installGuards(router, { t: (key: string) => i18n.t(key) });

// Register the programmatic "go to Cloud Model Settings" navigation for the
// guided missing-API-key flow. `useCloudModelStatus.openApiKeyFlow()` runs
// outside a component setup (e.g. from `useChatTurnSubmit`), so it cannot use
// `useRouter()`; App.vue owns the router and registers a callback once here.
registerCloudModelSettingsNavigator(() => {
  void router.push({ path: "/settings", query: { tab: "cloud-models" } });
});

useTheme();
useFontSize();

// Mount the command palette's Escape-to-close listener + command registry;
// the palette overlay is already mounted. Opening is bound to Ctrl/Cmd+.
// below (V1 app.js:2253). Ctrl/Cmd+K is left to the chat composer's
// model-selection dropdown (V1 app.js:2260).
useCommandPalette();

// Additional global keyboard shortcuts.
//
// V1 app.js:2248-2314 registered a single global keydown handler. V2 keeps
// the sidebar command-palette button as a deliberate enhancement, and here
// restores the V1 shortcuts that were lost in the rewrite:
//
//   • Ctrl/Cmd+.  → open the command palette (V1 app.js:2253). Fires even
//                    inside inputs (V1 parity: not gated on input focus).
//   • Ctrl/Cmd+/  → focus the chat composer textarea (V1: fires anywhere).
//   • Ctrl/Cmd+,  → Settings (existing V2 binding, retained).
//
// (Ctrl/Cmd+K → toggle model-selection dropdown is owned by ChatComposer,
//  V1 app.js:2260.)
//
// Platform-aware tab shortcuts (close / new IN-APP chat tab):
//   • Desktop (Tauri):  Ctrl/Cmd+W AND Alt+W → close active chat tab
//                       Ctrl/Cmd+N AND Alt+N → new chat tab
//     The Tauri shell installs no native menu/accelerator, so these keys
//     reach the page and `preventDefault()` reliably suppresses any default.
//     Both modifier variants are bound so muscle memory works either way.
//   • Browser (WebUI):  Alt+W → close active chat tab
//                       Alt+N → new chat tab
//     `Ctrl+W` (close browser tab) and `Ctrl+N` (new browser window) are
//     reserved browser/OS shortcuts that JS CANNOT intercept — browsers
//     ignore `preventDefault()` for them. So WebUI uses ONLY the
//     interceptable Alt+W / Alt+N. See utils/platform.ts for the rationale.
const desktop = isDesktopShell();
function closeActiveChatTab(event: KeyboardEvent): void {
  event.preventDefault();
  const id = chatTabs.activeTabId;
  if (id !== null) {
    // closeTab already falls back to a neighbouring tab and re-opens a
    // blank tab when the last one is closed (stores/chatTabs.ts).
    chatTabs.closeTab(id);
  }
}
function newChatTab(event: KeyboardEvent): void {
  event.preventDefault();
  chatTabs.openTab({ title: i18n.t("chat.tab.untitled") });
  void router.push({ name: "chat" });
}
const tabShortcuts: KeymapBinding[] = desktop
  ? [
      // Desktop (Tauri): both Ctrl/Cmd and Alt variants — all interceptable.
      { key: "w", ctrlOrMeta: true, skipInEditable: false, handler: closeActiveChatTab },
      { key: "w", alt: true, skipInEditable: false, handler: closeActiveChatTab },
      { key: "n", ctrlOrMeta: true, handler: newChatTab },
      { key: "n", alt: true, handler: newChatTab },
    ]
  : [
      // Browser (WebUI): Alt+W / Alt+N — Ctrl variants are browser-reserved.
      { key: "w", alt: true, skipInEditable: false, handler: closeActiveChatTab },
      { key: "n", alt: true, handler: newChatTab },
    ];
//
// Escape-driven overlay closing:
// - CommandPalette: handled by useCommandPalette (Escape closes it).
// - ConfirmDialog: handled by ConfirmDialog.vue (Escape closes it).
// - CloudModelsPanel side panel: handled by CloudModelsPanel.vue (Escape closes it).
// - PromptSnapshot modal: handled by ChatMessageList.vue (Escape closes it).
// - Lightbox: handled by useLightbox.onKeydown in each component.
// - AppSidebar popovers: handled by AppSidebar.vue.
// Global Escape binding here closes the command palette as the top-level
// fallback (V1 app.js:2281 chain — palette is the outermost overlay).
useKeymap([
  {
    // V1 app.js:2281 — Escape closes the topmost open overlay.
    // In V2 the command palette is the only globally-accessible overlay
    // from App.vue; other overlays handle their own Escape locally.
    key: "Escape",
    skipInEditable: false,
    handler: (_event: KeyboardEvent) => {
      if (palette.open) {
        palette.hide();
      }
    },
  },
  {
    key: ",",
    ctrlOrMeta: true,
    handler: (event: KeyboardEvent) => {
      event.preventDefault();
      void router.push({ name: "settings" });
    },
  },
  {
    // V1 app.js:2253 — Ctrl+. opens the command palette, even from inputs.
    key: ".",
    ctrlOrMeta: true,
    skipInEditable: false,
    handler: (event: KeyboardEvent) => {
      event.preventDefault();
      palette.show();
    },
  },
  {
    // Ctrl/Cmd+K also opens the command palette (modern convention).
    key: "k",
    ctrlOrMeta: true,
    skipInEditable: false,
    handler: (event: KeyboardEvent) => {
      event.preventDefault();
      palette.show();
    },
  },
  {
    // V1 app.js:2274 — Ctrl+/ focuses the chat composer textarea.
    key: "/",
    ctrlOrMeta: true,
    skipInEditable: false,
    handler: (event: KeyboardEvent) => {
      event.preventDefault();
      const ta = document.querySelector<HTMLTextAreaElement>(
        ".rich-input-textarea",
      );
      ta?.focus();
    },
  },
  // Platform-aware close/new IN-APP chat tab (see comment block above).
  // Desktop: Ctrl/Cmd+W / Ctrl/Cmd+N. Browser: Alt+W / Alt+N.
  ...tabShortcuts,
]);

onMounted(() => {
  // Hydrate the SSO snapshot BEFORE any protected API call fires so:
  //   * the router beforeEach guard's first `await auth.refresh()` hits
  //     an already-loaded store (no wait on the first navigation);
  //   * the sidebar user button appears in the same paint as the rest
  //     of the chrome, not after a visible "auth loading" gap;
  //   * if the gate is on and no session cookie is present, we redirect
  //     to /auth/login BEFORE the SSE / conversation fetches attach —
  //     otherwise EventSource would race the redirect, 401 once, and
  //     leave a stray retry line in the backend log every ~3s until the
  //     browser navigates away.
  //
  // Serialised on purpose (await inside an async IIFE, then guard the
  // rest of the mount work). If `fetchAuthMe` fails the store
  // gracefully reports `auth_enabled=false` (see api/auth.ts) so this
  // branch degrades to "run everything unconditionally", which matches
  // the pre-SSO behaviour.
  // Register the "authentication required" handler so any protected API
  // call that 401s (session expired mid-use) raises the in-app login
  // prompt modal instead of hard-redirecting the page.
  setAuthRequiredHandler(() => {
    // The server has rejected the session (401 auth.required). Mark local
    // state as unauthenticated so the promptLogin() guard passes and the
    // LoginPrompt modal actually renders. Without this, the store still
    // thinks `authenticated=true` (stale client-side snapshot) and the
    // modal never appears — the user sees a broken/empty UI with no
    // indication that they need to re-sign-in.
    auth.authenticated = false;
    auth.promptLogin();
  });

  void (async () => {
    await auth.refresh();
    if (auth.authEnabled && !auth.authenticated) {
      // Do NOT hard-redirect to Okta on load (jarring). Render the SPA
      // and raise the in-app login prompt modal instead. The SPA behind
      // it is inert (every business API 401s) but visible, which is a
      // far softer first impression than a bounce to account.qualcomm.com.
      auth.promptLogin();
    } else if (auth.authEnabled && auth.authenticated) {
      // Welcome-back toast + start the keep-alive renewal timer.
      const name = auth.user?.display_name || auth.user?.username || "";
      toast.success(i18n.t("auth.welcome_back", { name }));
      startSessionKeepAlive();
    }
    // Always continue mounting — the SPA renders whether or not the user
    // is signed in (the modal gates interaction, not rendering).
    startAppMount();
  })();
});

// ── Session keep-alive ─────────────────────────────────────────────────
// Poll on a coarse interval; when the session is within
// ``RENEW_THRESHOLD_S`` of expiry, slide it forward via POST
// /api/auth/renew so an active user is never kicked out mid-task. The
// timer is torn down on unmount. If the tab is backgrounded and the
// session lapses anyway, the next business call 401s → login prompt.
const RENEW_THRESHOLD_S = 10 * 60; // renew when < 10 min left
const KEEPALIVE_INTERVAL_MS = 60 * 1000; // check every minute
let keepAliveTimer: ReturnType<typeof setInterval> | null = null;

function startSessionKeepAlive(): void {
  if (keepAliveTimer !== null) return;
  keepAliveTimer = setInterval(() => {
    const left = auth.secondsUntilExpiry;
    if (left !== null && left <= RENEW_THRESHOLD_S) {
      void auth.renew();
    }
  }, KEEPALIVE_INTERVAL_MS);
}

function stopSessionKeepAlive(): void {
  if (keepAliveTimer !== null) {
    clearInterval(keepAliveTimer);
    keepAliveTimer = null;
  }
}

/**
 * The remainder of the original `onMounted` body — extracted into a
 * function so the SSO gate above can gate SSE / conversation wiring on
 * the auth snapshot. Called exactly once, either directly (SSO off /
 * already signed in) or after `auth.refresh()` resolves.
 */
function startAppMount(): void {
  // Fetch the edition flag once (internal vs external). Best-effort and
  // non-blocking: it feeds the guided missing-cloud-API-key flow's
  // internal-vs-external branch (useCloudModelStatus.openApiKeyFlow); on
  // failure `isInternal` stays null and the flow routes to Settings.
  void service.fetchEdition();

  // Pull the cloud-model permission snapshot once at app mount. The backend
  // lifespan spawns the actual probe scan asynchronously (one GET /v1/models
  // per configured cloud provider, comparing configured vs returned model
  // ids to derive per-model allowed/denied). Best-effort and fail-open: a
  // network error / not-yet-populated snapshot leaves the store empty →
  // every model shows in the dropdown (never-preset-unavailable). Fired
  // AFTER `service.fetchEdition()` so it does not compete with the
  // edition-detection round-trip on a cold start (both are non-blocking).
  void cloudModelPermissions.refresh();

  // Connect the global SSE stream (`/api/events`). V1 connected this on app
  // mount (useChat.js:connectEventStream). V2 consumes the `reboot` event here
  // plus the `permission_request` event (FileGuard ASK → authorization
  // dialog); other event types are handled by their own feature
  // subscriptions (channel sync, ...).
  disconnectGlobalEvents = connectGlobalEvents({
    onEvent(evt) {
      if (evt.type === "reboot") {
        beginReboot();
      } else if (evt.type === "permission_request") {
        // The SSE frame is the V1-shaped permission request
        // (id / op / path / caller / channel / session_id / timestamp);
        // forward it to the shared queue (de-dupes by id).
        permissionDialog.enqueue(
          evt as unknown as Parameters<typeof permissionDialog.enqueue>[0],
        );
      } else if (
        evt.type === "wechat_update_conv" ||
        evt.type === "feishu_update_conv"
      ) {
        // A Feishu / WeChat message arrived (or its reply was sent): the
        // backend already persisted the channel conversation (title
        // `[飞书]` / `[微信]` + round/tool counts + `meta.source`). Refresh
        // the sidebar "Recent conversations" list so the new conversation /
        // updated turn appears INSTANTLY, mirroring V1's live update
        // (`useChat.js:2935-2994` inserted the row keyed by `conv_id`). We
        // refetch (rather than reconstruct the summary client-side) because
        // the frame carries only the new message, not the full summary with
        // badges — a refetch surfaces the authoritative row. Gated on a real
        // `conv_id` (V1 `useChat.js:2938`): a frame without one cannot be
        // addressed. Best-effort; degrades silently.
        const convId =
          typeof evt.conv_id === "string" ? evt.conv_id : "";
        if (convId !== "") {
          void conversations.fetch();
        }
      }
    },
    onOpen() {
      // SSE (re)connect: pull permission requests the backend still holds so
      // any pushed while disconnected are not lost (V1 `security:sse_connected`
      // → fetchPending). Best-effort; degrades silently.
      void permissionDialog.fetchPending();
    },
    onError() {
      // The SSE connection dropped. This happens both on transient network
      // blips AND when the backend process exits for a reboot (exit 75).
      // In the reboot case the backend never gets to push a `reboot` SSE
      // frame before it shuts down, so `beginReboot()` is never called via
      // `onEvent` — the overlay never appears and the user has to refresh
      // manually.
      //
      // Fix: on every SSE error, probe `/api/system/health` once. If the
      // probe itself fails (connection refused / timeout) the backend is
      // definitely down → enter the reboot transition immediately so the
      // overlay shows and health-polling takes over. If the probe succeeds
      // the backend is still up (transient blip) → do nothing.
      void (async () => {
        if (_isRebootProbing) return; // one probe at a time
        _isRebootProbing = true;
        const base = apiBaseUrl();
        const url = base
          ? `${base}/api/system/health`
          : "/api/system/health";
        const _probeAbort = new AbortController();
        const _probeTimeout = setTimeout(() => _probeAbort.abort(), 5000);
        try {
          const res = await fetch(url, { method: "GET", signal: _probeAbort.signal });
          if (!res.ok) beginReboot(); // non-2xx → server in bad state
        } catch {
          // fetch threw → server is unreachable → reboot transition
          beginReboot();
        } finally {
          clearTimeout(_probeTimeout);
          _isRebootProbing = false;
        }
      })();
    },
  });

  // Force the route guard to run once for the initial navigation
  // (afterEach does not fire for the very first sync resolution in
  // some race conditions). This is idempotent.
  if (typeof document !== "undefined" && router.currentRoute.value !== null) {
    const base = i18n.t("app.title");
    const meta = router.currentRoute.value.meta as { titleKey?: string };
    if (meta.titleKey !== undefined) {
      const view = i18n.t(meta.titleKey);
      document.title =
        view === meta.titleKey || view === "" ? base : `${view} · ${base}`;
    } else {
      document.title = base;
    }
  }
}

onBeforeUnmount(() => {
  disconnectGlobalEvents?.();
  disconnectGlobalEvents = null;
  stopSessionKeepAlive();
  setAuthRequiredHandler(null);
});

</script>

<template>
  <div :data-theme="ui.resolvedTheme">
    <div class="app-layout">
      <AppSidebar />
      <!-- Mobile-only backdrop: tap to close the slide-in sidebar.
           Pairs with the `.mobile-sidebar-backdrop` rules in layout.css and
           the `.mobile-open` state on AppSidebar. -->
      <div
        v-if="ui.mobileSidebarOpen"
        class="mobile-sidebar-backdrop"
        @click="ui.setMobileSidebarOpen(false)"
      ></div>
      <div class="main-content">
        <AppHeader />
        <AppMain />
      </div>
    </div>
    <AppToastHost />
    <ConfirmDialog />
    <AppCommandPalette />
    <RebootOverlay />
    <SecurityDialog />
    <LoginPrompt />
  </div>
</template>

<style scoped>
</style>
