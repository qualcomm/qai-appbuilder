// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useReboot` — service-restart transition controller (V1 parity).
 *
 * Restores the V1 reboot experience that regressed in the V2 rewrite:
 *   - V1 `app.js:3382-3395` (`triggerReboot`) — confirm dialog, then
 *     `POST /api/reboot`.
 *   - V1 `useChat.js:2851-2900` — on the `reboot` SSE event (or right after
 *     the POST) show a full-screen overlay, poll `/api/health` every 2s,
 *     and `window.location.reload()` once the service is healthy again.
 *   - V1 `index.html:128-135` — the `.reboot-overlay` markup.
 *
 * V2 had degraded to a fire-and-forget `fetch` with no overlay / polling /
 * auto-refresh, and the locale hint had been changed from "auto-refresh" to
 * "please refresh manually".
 *
 * Architecture (判据 1): all transition state + side-effects are converged
 * here as a module-scoped singleton so the overlay component (`RebootOverlay`),
 * the sidebar button, and the chat `/reboot` command share one source of
 * truth. Components stay thin — they only read `isRebooting` / call
 * `requestReboot()`. The actual POST is delegated to the existing service
 * Pinia store (`/api/system/reboot`); polling hits `/api/system/health`
 * directly (mirroring V1's bare `fetch('/api/health')`, deliberately bypassing
 * the store's `error` side-effect because failed polls are expected while the
 * daemon is down).
 */
import { ref, readonly, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { useServiceStore } from "@/stores/service";
import { useConfirm } from "@/composables/useConfirm";
import { apiBaseUrl } from "@/api/base";

// V1 useChat.js:2863 — poll cadence.
const POLL_INTERVAL_MS = 2000;

// Module-scoped singleton state (shared across all callers).
const _isRebooting = ref(false);
let _pollTimer: ReturnType<typeof setInterval> | null = null;

function _healthUrl(): string {
  const base = apiBaseUrl();
  return base ? `${base}/api/system/health` : "/api/system/health";
}

/**
 * Begin the reboot transition: show the overlay and start polling health.
 *
 * Idempotent — calling it again while already rebooting is a no-op (so an
 * incoming `reboot` SSE event after a locally-initiated reboot won't double
 * up the poll timer).
 */
function beginReboot(): void {
  if (_isRebooting.value) return;
  _isRebooting.value = true;
  _startHealthPolling();
}

function _startHealthPolling(): void {
  if (_pollTimer !== null) {
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
  // V1 useChat.js:2854-2863 — every 2s hit /api/health; on the first OK
  // response, stop polling and hard-refresh the page.
  _pollTimer = setInterval(() => {
    void (async () => {
      try {
        const res = await fetch(_healthUrl(), { method: "GET" });
        if (res.ok) {
          if (_pollTimer !== null) {
            clearInterval(_pollTimer);
            _pollTimer = null;
          }
          window.location.reload();
        }
      } catch {
        // Service not back yet — keep waiting (V1 parity).
      }
    })();
  }, POLL_INTERVAL_MS);
}

export function useReboot(): {
  isRebooting: Readonly<Ref<boolean>>;
  requestReboot: () => Promise<void>;
  requestRebootDirect: () => Promise<void>;
  beginReboot: () => void;
} {
  const service = useServiceStore();
  const { confirm } = useConfirm();
  const { t } = useI18n();

  /**
   * Confirm + trigger a reboot from a UI control (sidebar button / chat
   * `/reboot`). V1 `app.js:3382-3395`: show a danger confirm; on accept,
   * POST the reboot, then enter the overlay/poll transition. The POST is
   * expected to drop the connection as the daemon exits (REBOOT_EXIT_CODE=75),
   * so connection errors are swallowed.
   */
  async function requestReboot(): Promise<void> {
    const ok = await confirm({
      // V1 reused app.rebootTitle/Message; V2 has a dedicated `reboot.*` ns.
      title: t("reboot.title"),
      message: t("reboot.confirmMessage"),
      confirmText: t("reboot.confirm"),
      cancelText: t("reboot.cancel"),
      icon: "🔄",
      confirmStyle: "danger",
    });
    if (!ok) return;

    // Enter the transition first so the overlay is visible immediately, even
    // if the POST connection is severed mid-flight by the daemon exit.
    beginReboot();
    try {
      await service.reboot();
    } catch {
      // Service shutting down — connection break is expected (V1 parity).
    }
  }

  /**
   * Trigger a reboot WITHOUT a confirm dialog — for the chat `/reboot`
   * command (V1 `useChat.js:1613-1628`, which POSTs directly with no confirm;
   * the typed command is itself the user's confirmation). Enters the same
   * overlay/poll transition.
   */
  async function requestRebootDirect(): Promise<void> {
    beginReboot();
    try {
      await service.reboot();
    } catch {
      // Service shutting down — connection break is expected (V1 parity).
    }
  }

  return {
    isRebooting: readonly(_isRebooting),
    requestReboot,
    requestRebootDirect,
    beginReboot,
  };
}
