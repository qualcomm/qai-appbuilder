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
import { useQuotaStore } from "@/stores/quota";
import { useNotificationsStore } from "@/stores/notifications";
import { useTheme } from "@/composables/useTheme";
import { useFontSize } from "@/composables/useFontSize";
import { useCommandPalette } from "@/composables/useCommandPalette";
import { useToast } from "@/composables/useToast";
import { useKeymap, type KeymapBinding } from "@/composables/keymap";
import { isDesktopShell } from "@/utils/platform";
import { useReboot } from "@/composables/useReboot";
import { usePermissionDialog } from "@/composables/security/usePermissionDialog";
import { connectGlobalEvents } from "@/api/globalEvents";
import { resumeConversationIfRunning } from "@/composables/chat/useActiveChatRunAttach";
import { apiBaseUrl } from "@/api/base";
import { setAuthRequiredHandler, releaseAuthGate } from "@/api/http";
import { silentLogin } from "@/api/auth";
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
import GalleryUploadIndicator from "@/components/app-builder/model-builder/GalleryUploadIndicator.vue";

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
// Token-pool balance (QAI Service) — seeded once at mount below, then kept
// current by the `quota_usage` stream frame. Empty store ⇒ gauge hidden.
const quotaStore = useQuotaStore();
// Toast host is mounted globally (AppToastHost); use it for the
// welcome-back message after a successful sign-in.
const toast = useToast();
// Persistent unread notification center (方案乙): scheduled-task results
// arriving on `scheduling.task_fired` are enqueued here as durable unread
// entries (the bell list), plus a one-shot toast for immediacy.
const notifications = useNotificationsStore();
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
    // The guards live in ``promptLogin()`` so this handler and the router
    // guard cannot drift apart: it ignores the 401 when a sign-in is still in
    // flight, or when one just succeeded. Duplicating a hard-coded 3s window
    // here is what let a mid-login 401 through — a real sign-in takes ~8s, so
    // the window had already lapsed while Okta was still being contacted.
    if (auth.isLoginInFlight) return;
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
      // NOTE: auth gate stays armed — business requests won't fire until
      // the user signs in (prevents the 401 flood in the console).
    } else {
      // Auth disabled or user authenticated — release the gate so all
      // business API requests can proceed immediately.
      releaseAuthGate();
      if (auth.authEnabled && auth.authenticated) {
        // Welcome-back toast + start the keep-alive renewal timer.
        const name = auth.user?.display_name || auth.user?.username || "";
        toast.success(i18n.t("auth.welcome_back", { name }));
        startSessionKeepAlive();
      }
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

// ── Tab-visibility guard ───────────────────────────────────────────────
// Browsers throttle/pause timers in backgrounded tabs. When the user
// returns, the session may be close to (or past) expiry. Immediately
// check and renew on visibility restoration so the keep-alive never
// misses its window.
function onVisibilityChange(): void {
  if (document.hidden) return;
  // Tab just became visible — immediate check.
  if (!auth.authEnabled || !auth.authenticated) return;
  const left = auth.secondsUntilExpiry;
  if (left === null) return;
  if (left <= RENEW_THRESHOLD_S) {
    void auth.renew();
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", onVisibilityChange);
}

// Start the keep-alive timer when auth flips to authenticated (covers
// re-login via LoginPrompt — the initial mount path calls it directly).
watch(
  () => auth.authenticated,
  (now, prev) => {
    if (now && !prev) startSessionKeepAlive();
    if (!now && prev) stopSessionKeepAlive();
  },
);

/**
 * Seed the token-pool gauge, silently re-authenticating when the ONLY thing
 * missing is the broker session token.
 *
 * The pool's bearer is minted only inside the Okta `/callback`. It is now
 * persisted (OS keyring) so a restart with a still-valid token is transparent
 * — `seed()` reads it back and returns `"ok"`, and this recovery never fires.
 * But two states leave a valid session cookie with NO usable token: a restart
 * after the persisted token has expired, and a first launch that never minted
 * one. In both, `/api/quota/me` answers `"no_session_token"`.
 *
 * `silentLogin({ reexchange: true })` fixes exactly that. `reexchange` forces
 * the backend past its "already signed in" short-circuit so the flow actually
 * reaches `/callback` and re-mints the token; when the Okta IdP session is
 * still live the whole chain is 302s inside a hidden iframe, so the user sees
 * nothing. The freshly minted token is also re-persisted (store path), so the
 * next restart is back on the fast path.
 *
 * Deliberately narrow:
 *   * `"no_session_token"` is the only trigger. `"not_configured"` means the
 *     pool is not deployed (nothing to recover); `"unavailable"` is a
 *     transient fault re-authentication would not fix.
 *   * Only when the gate is on and we believe we are signed in — otherwise the
 *     LoginPrompt owns the flow and a competing silent attempt is noise.
 *   * Exactly one attempt, no retry loop: if the Okta IdP session has ALSO
 *     expired the silent path cannot succeed (Okta would need to render a
 *     login page, which a hidden iframe cannot show), and the visible sign-in
 *     path is the LoginPrompt's job, not a background task's.
 *   * Never surfaces an error. Failing here leaves precisely the prior
 *     behaviour (no gauge), so this is a pure improvement or a no-op.
 */
// In-flight guard: two concurrent silent-login attempts would both hit
// ``/callback`` and each mint a new JWT, leaving the store racing between
// two valid-but-different bearers for the same user. In practice one
// caller today (mount), but a fast tab reload / HMR reload can double-fire
// the effect; keeping this at module scope survives both cases.
let _seedInFlight: Promise<void> | null = null;
async function seedQuotaWithRecovery(): Promise<void> {
  if (_seedInFlight !== null) {
    // A recovery is already running; join it instead of racing a second
    // hidden iframe. Callers only care that "seed has been attempted".
    return _seedInFlight;
  }
  const run = async (): Promise<void> => {
    const result = await quotaStore.seed();
    if (result !== "no_session_token") return;
    if (!auth.authEnabled || !auth.authenticated) return;
    // Bracket the attempt so a 401 arriving mid-flow does not raise the
    // login prompt on top of a sign-in that is already running.
    auth.beginLogin();
    try {
      await silentLogin({ reexchange: true });
    } catch {
      // IdP session gone / iframe blocked. Leave the gauge hidden; the user
      // will be prompted by the normal auth path when they next act.
      return;
    } finally {
      auth.endLogin();
    }
    // The callback ran and re-minted the token — read the balance again.
    await quotaStore.seed();
  };
  _seedInFlight = run().finally(() => {
    _seedInFlight = null;
  });
  return _seedInFlight;
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

  // Token-pool balance (QAI Service) — seed the sidebar gauge once. A freshly
  // opened window has streamed nothing yet, so it has no balance to show; from
  // here on the `quota_usage` stream frame keeps it current for free (no
  // polling). Best-effort and silent: when the pool is not deployed, or the
  // user holds no broker token, the store stays empty and the gauge simply
  // does not render.
  //
  // One case is worth recovering from rather than rendering as "no pool":
  // `"no_session_token"`. The pool's bearer is minted only inside the Okta
  // /callback, so a valid session cookie can coexist with a missing broker
  // token (backend restarted with an expired persisted token, or a first
  // launch that never had one). `seedQuotaWithRecovery()` closes that gap with
  // a silent re-exchange. This composes with the persisted-JWT path: `seed()`
  // reads the stored token first, so recovery only fires when there is nothing
  // usable to restore.
  void seedQuotaWithRecovery();

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
      } else if (evt.type === "permission_resolved") {
        // Problem ② — the backend resolved a PENDING native ASK WITHOUT a
        // local user response (chat "Stop" exec-cancel flush, or the
        // subprocess-gone cleanup backstop) and pushed this UI-close signal.
        // Dequeue the matching dialog by id so the FileGuard authorization
        // window closes immediately instead of continuing to pop up. Guard on
        // a non-empty string id (a malformed frame is ignored).
        const resolvedId = typeof evt.id === "string" ? evt.id : "";
        if (resolvedId !== "") {
          permissionDialog.dequeue(resolvedId);
        }
      } else if (
        evt.type === "background_process.updated" ||
        evt.type === "background_process.deleted"
      ) {
        // Tab-level busy state (§UX-idle-with-bg-work). The process CARDS
        // subscribe to these same frames for their own badge, but a card only
        // exists while its message is on screen — the typing indicator and
        // Stop button need a tab-level answer to "is the agent still
        // working?". Mirroring the frames into the store's index here reuses
        // the subscription that already exists rather than adding a poller.
        // Payload shapes differ between the two frames (see
        // ``background_process/events.py``): ``updated`` carries the whole
        // ``info`` record, ``deleted`` carries only ``process_id``. Parsed
        // the same way ``BackgroundProcessCard`` does so there is one
        // reading of these frames, not two.
        const bgpInfo = evt["info"] as
          | { id?: unknown; status?: unknown; session_id?: unknown }
          | undefined;
        const bgpId =
          typeof bgpInfo?.id === "string"
            ? bgpInfo.id
            : typeof evt["process_id"] === "string"
              ? (evt["process_id"] as string)
              : "";
        if (bgpId !== "") {
          chatTabs.noteBackgroundProcessState(
            bgpId,
            evt.type === "background_process.deleted"
              ? null
              : typeof bgpInfo?.status === "string"
                ? bgpInfo.status
                : null,
            // Owning conversation, so the busy marker lands on the tab that
            // STARTED the process rather than whichever tab is on screen when
            // the frame arrives. `deleted` carries no `info` (see
            // ``background_process/events.py``) and needs none — retirement
            // sweeps every tab.
            typeof bgpInfo?.session_id === "string"
              ? bgpInfo.session_id
              : undefined,
          );
        }
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
      } else if (evt.type === "scheduling.task_fired") {
        // A scheduled task finished its isolated agent run. What is durable
        // depends on scope:
        //   • CONVERSATION-bound task: the backend already persisted the
        //     result as a real assistant turn in the target conversation, but
        //     the headless runner drains that turn's frames itself — the live
        //     `ChatStream*` frames are dropped at the use case's `_publish`
        //     source and only reach a client via the SSE/WS route the runner
        //     bypasses — so an already-open tab is NOT updated live and the
        //     turn appears only on a manual reload. We therefore force the
        //     open tab (if any) bound to that conversation to re-fetch its
        //     newest page, so the fired turn shows immediately.
        //   • GLOBAL task (`conversation_id === ""`): no conversation, so no
        //     persisted turn and nothing to reload — the result lives ONLY in
        //     the task's run history, which the notification center fetches on
        //     demand to show the full text inline.
        // In both cases we also:
        //   1. enqueue a DURABLE unread notification (the bell list, 方案乙) so
        //      the user still sees "a result arrived" even if the tab is closed;
        //   2. fire a ONE-SHOT toast for immediacy (auto-dismisses).
        // We do NOT re-append the result as a synthetic message here (that
        // would duplicate the persisted turn once the reload lands).
        const schedTaskId =
          typeof evt.task_id === "string" ? evt.task_id : "";
        const schedConvId =
          typeof evt.conversation_id === "string" ? evt.conversation_id : "";
        const schedOk = evt.ok === true;
        const schedName =
          typeof evt.task_name === "string" && evt.task_name !== ""
            ? evt.task_name
            : schedTaskId;
        const schedResult =
          typeof evt.result_text === "string" ? evt.result_text : "";
        // De-dup key MUST be the server-side run row id so a live delivery
        // and a later reconnect-backfill of the SAME fire coalesce into ONE
        // bell entry (see notifications.ts). Legacy events without run_id
        // fall back to task_id + timestamp, which never de-dups against the
        // backfill — acceptable because production always ships run_id.
        const schedRunId =
          typeof evt.run_id === "string" && evt.run_id !== ""
            ? evt.run_id
            : `${schedTaskId}-${Date.now().toString()}`;
        const schedTs = Date.now();
        notifications.enqueue({
          id: schedRunId,
          taskId: schedTaskId,
          taskName: schedName,
          ok: schedOk,
          conversationId: schedConvId,
          isGlobal: schedConvId === "",
          resultPreview: schedResult.slice(0, 200),
          ts: schedTs,
        });
        if (schedConvId !== "") {
          void chatTabs.reloadConversationMessages(schedConvId);
        }
        const schedHead = i18n.t(
          schedOk ? "chat.scheduledTaskRan" : "chat.scheduledTaskFailed",
          { name: schedName },
        );
        toast.info(schedHead);
      } else if (evt.type === "chat.started_stream") {
        // A chat turn just started somewhere on the backend.  For a
        // USER-initiated turn the primary chat WS (`useChatTransport`)
        // is already open and has flipped the tab to ``streaming`` —
        // we do NOT want to open a second attach socket in that case
        // (would race with the primary and duplicate frames).
        //
        // For a HEADLESS follow-up (the coordinator spun a new turn
        // after a background job settled), no primary WS is open and
        // the tab is still ``idle``.  Attach
        // ``/api/chat/active-runs/{tab_id}/ws`` so the UI streams the
        // headless turn live INSTEAD OF waiting for a manual reload.
        // ``resumeConversationIfRunning`` promotes the tab to
        // ``streaming`` and calls ``attachActiveChatRun`` (which is
        // idempotent for de-dup safety).
        // ``ChatStreamStartedEvent`` uses ``TabId`` / ``ConversationId``
        // value objects (dataclasses with a single ``value`` field), so
        // the wire shape is ``{ value: "tab-abc" }`` — NOT a plain
        // string.  Extract defensively (accept either shape so this
        // stays working if backend flattens them one day).
        const extractId = (raw: unknown): string => {
          if (typeof raw === "string") return raw;
          if (
            raw !== null &&
            typeof raw === "object" &&
            "value" in raw &&
            typeof (raw as { value: unknown }).value === "string"
          ) {
            return (raw as { value: string }).value;
          }
          return "";
        };
        const startedTabId = extractId(evt.tab_id);
        const startedConvId = extractId(evt.conversation_id);
        if (startedTabId !== "" && startedConvId !== "") {
          const startedTab = chatTabs.tabs.find((t) => t.id === startedTabId);
          // Only attach for tabs that are open in this browser AND
          // currently idle (a user turn's primary WS has already
          // flipped the tab to ``streaming`` before this event lands;
          // a headless follow-up leaves the tab ``idle``).
          if (startedTab !== undefined && startedTab.status !== "streaming") {
            void resumeConversationIfRunning(startedTabId, startedConvId);
          }
        }
      }
    },
    onOpen() {
      // WS/SSE (re)connect: pull anything the durable transports may have
      // dropped while we were disconnected. Two independent surfaces both
      // need this backfill because their WS deliveries are optimistic:
      //   * permissionDialog.fetchPending — permission requests pushed
      //     while disconnected (V1 `security:sse_connected`).
      //   * notifications.fetchUnread — scheduled-task fires whose
      //     `scheduling.task_fired` WS event was lost. The bell is now a
      //     PROJECTION of the ``scheduling_task_run`` table (see migration
      //     075) and every reconnect merges its unread rows into the local
      //     store, so a fire that landed while WS was silently dead surfaces
      //     the next time the tab regains connectivity.
      // Both are best-effort — a fetch failure degrades to the WS-only fast
      // path, which is what we had before.
      void permissionDialog.fetchPending();
      void notifications.fetchUnread();
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
  if (typeof document !== "undefined") {
    document.removeEventListener("visibilitychange", onVisibilityChange);
  }
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
    <GalleryUploadIndicator />
  </div>
</template>

<style scoped>
</style>
