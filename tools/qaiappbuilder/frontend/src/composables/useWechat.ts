// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useWechat` — single-instance WeChat channel + QR login (V1-aligned).
 *
 * UI/flow mirrors the V1 verified single-instance experience
 * (`channels/wechat/useWechat.js`): the user clicks "Connect WeChat", a QR
 * code appears with a countdown + manual refresh, and we poll until the
 * scan is confirmed. There is NO instance list / register form — WeChat is
 * a single instance.
 *
 * The V2 backend is internally multi-instance (every instance is a
 * registered ULID; there is NO `default` and NO "list by kind" endpoint, and
 * register does NOT de-dup). To present the V1 single-instance experience we
 * keep the instance_id transparent:
 *   • persist the instance_id in localStorage (guarded with try/catch)
 *   • on connect: reuse the stored id if `GET /api/wechat/status` still finds
 *     it (200); otherwise `POST /api/wechat/register` once (placeholder
 *     credentials — QR scan login does not depend on app secrets) and store
 *     the returned ULID.
 *
 * Backend contract (TestClient-verified, interfaces/http/routes/channels.py):
 *   POST /api/wechat/register {name,secret_service,secret_key,secret_value,metadata} → instance
 *   GET  /api/wechat/status?instance_id=               → { instance{status}, health{status} }
 *   POST /api/wechat/qr/issue {instance_id}            → { challenge_id, status, expires_at }
 *   GET  /api/wechat/qr/{cid}/status?instance_id=      → { status }  (issued|scanned|confirmed|expired)
 *   GET  /api/wechat/qr/{cid}/image?instance_id=       → image/png
 *   POST /api/wechat/qr/{cid}/confirm {instance_id}    → { status }
 */
import { inject, provide, ref, type InjectionKey } from "vue";
import { useI18n } from "vue-i18n";

import { apiJson, ApiError, buildApiUrl } from "@/api";
import { useToastStore } from "@/stores/toast";

// ─── Types ───────────────────────────────────────────────────────────────────

/**
 * V1-aligned synthesized connection state (`useWechat.js:11`):
 *   idle | logging_in | scanned | connected | expired | error
 * Synthesized in V2 from `instance.status` (stopped/running) + the active QR
 * challenge status (issued/scanned/confirmed/expired).
 */
export type WechatStatus =
  | "idle"
  | "logging_in"
  | "scanned"
  | "connected"
  | "expired"
  | "error";

interface ChannelInstanceResponse {
  instance_id: string;
  kind: string;
  name: string;
  status: string;
  last_error: string;
  created_at: string;
  updated_at: string;
  metadata: Record<string, string>;
}

interface StatusResponse {
  instance: ChannelInstanceResponse;
  health: Record<string, unknown>;
}

interface QrChallengeResponse {
  challenge_id: string;
  instance_id: string;
  status: string;
  issued_at: string;
  expires_at: string;
}

interface ConfigResponse {
  auto_start: boolean;
  kind_specific: Record<string, string>;
}

const LS_KEY = "qai.wechat.instance_id";

// ─── localStorage helpers (guarded; never throw) ──────────────────────────────

function loadStoredInstanceId(): string | null {
  try {
    return localStorage.getItem(LS_KEY);
  } catch {
    return null;
  }
}

function storeInstanceId(id: string): void {
  try {
    localStorage.setItem(LS_KEY, id);
  } catch {
    // Storage unavailable (private mode / quota) — keep id in memory only.
  }
}

// ─── Composable ──────────────────────────────────────────────────────────────

export function useWechat() {
  const wechatStatus = ref<WechatStatus>("idle");
  const wechatQrUrl = ref<string | null>(null);
  const wechatQrLoading = ref(false);
  const wechatQrCountdown = ref(0);
  const loading = ref(false);
  // V1 parity: idle-state "auto connect on service start" toggle + its save
  // flag. Defaults ON to match V1's factory default
  // (forge_config `wechat_channel.auto_connect: true`); the backend per-kind
  // factory default (`ChannelInstance.get_settings`) agrees once an instance
  // exists, and `loadWechatConfig` back-fills the persisted value thereafter.
  const wechatAutoConnect = ref(true);
  const wechatConfigSaving = ref(false);

  let instanceId: string | null = loadStoredInstanceId();
  let challengeId: string | null = null;
  let pollHandle: ReturnType<typeof setInterval> | null = null;
  let countdownHandle: ReturnType<typeof setInterval> | null = null;
  // Cache-busting reload loop for the SDK-driven QR <img>: the qr-image
  // endpoint 404s until the wechatbot SDK reports a URL via on_qr_url, so we
  // periodically bump the `t=` param to force the <img> to re-fetch until the
  // real QR PNG is available (V1 parity: V1's 2s status poll surfaced qr_url
  // the moment it arrived).
  let qrImageHandle: ReturnType<typeof setInterval> | null = null;

  const { t } = useI18n();

  function toastError(msg: string): void {
    useToastStore().push({
      id: crypto.randomUUID(),
      kind: "error",
      message: msg,
      timeoutMs: 5000,
    });
  }

  function toastSuccess(msg: string): void {
    useToastStore().push({
      id: crypto.randomUUID(),
      kind: "success",
      message: msg,
      timeoutMs: 3000,
    });
  }

  // ── Instance resolution (transparent single-instance) ─────────────────────

  /** Verify the stored instance_id still exists on the backend (200). */
  async function instanceExists(id: string): Promise<boolean> {
    try {
      await apiJson<StatusResponse>("GET", "/api/wechat/status", undefined, {
        query: { instance_id: id },
      });
      return true;
    } catch {
      return false;
    }
  }

  /** Register a fresh WeChat instance (placeholder creds; QR doesn't need them). */
  async function registerInstance(): Promise<string | null> {
    try {
      const res = await apiJson<ChannelInstanceResponse>(
        "POST",
        "/api/wechat/register",
        {
          name: "WeChat",
          secret_service: "wechat",
          secret_key: "wechat-default",
          secret_value: "placeholder",
          metadata: {},
        },
      );
      return res.instance_id;
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : t("wechat.registerFailed", "Failed to register WeChat"));
      return null;
    }
  }

  /**
   * Resolve the single WeChat instance id, reusing the stored one if it still
   * exists, else registering exactly one (never blindly re-register — the
   * backend does not de-dup).
   */
  async function resolveInstanceId(): Promise<string | null> {
    if (instanceId !== null && (await instanceExists(instanceId))) {
      return instanceId;
    }
    const fresh = await registerInstance();
    if (fresh !== null) {
      instanceId = fresh;
      storeInstanceId(fresh);
    }
    return fresh;
  }

  // ── Status polling (V1 loadWechatStatus + startWechatPolling) ─────────────

  function stopPolling(): void {
    if (pollHandle !== null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
    stopQrImageReload();
  }

  function stopQrImageReload(): void {
    if (qrImageHandle !== null) {
      clearInterval(qrImageHandle);
      qrImageHandle = null;
    }
  }

  function stopCountdown(): void {
    if (countdownHandle !== null) {
      clearInterval(countdownHandle);
      countdownHandle = null;
    }
    wechatQrCountdown.value = 0;
  }

  /** Map the backend instance.status to the V1 synthesized status. */
  function applyInstanceStatus(instStatus: string): void {
    if (instStatus === "running") {
      wechatStatus.value = "connected";
    } else if (instStatus === "error") {
      wechatStatus.value = "error";
    }
    // stopped/starting/stopping while a challenge is active → keep QR state.
  }

  /** Load the channel connection status (V1 `loadWechatStatus`). */
  async function loadWechatStatus(): Promise<void> {
    if (instanceId === null) {
      const stored = loadStoredInstanceId();
      if (stored === null) return; // never connected → stay idle
      instanceId = stored;
    }
    try {
      const res = await apiJson<StatusResponse>(
        "GET",
        "/api/wechat/status",
        undefined,
        { query: { instance_id: instanceId } },
      );
      applyInstanceStatus(res.instance.status);
    } catch {
      // 404 = instance gone (e.g. data reset) → treat as idle.
      wechatStatus.value = "idle";
    }
  }

  // ── Config: auto-connect toggle (V1 loadWechatConfig / saveWechatConfig) ───

  /**
   * Load the WeChat config and back-fill the auto-connect toggle (V1
   * `loadWechatConfig` → `auto_connect`). The V2 backend stores this as the
   * generic `auto_start` flag on the channel instance config.
   */
  async function loadWechatConfig(): Promise<void> {
    if (instanceId === null) {
      const stored = loadStoredInstanceId();
      if (stored === null) return; // not registered yet → keep V1 default (on)
      instanceId = stored;
    }
    try {
      const res = await apiJson<ConfigResponse>("GET", "/api/wechat/config", undefined, {
        query: { instance_id: instanceId },
      });
      wechatAutoConnect.value = !!res.auto_start;
    } catch {
      // Unregistered instance → keep default (graceful, V1 parity).
    }
  }

  /**
   * Save the auto-connect toggle (V1 `saveWechatConfig`). Resolves/creates
   * the single instance first (QR login does not need credentials, so a
   * placeholder register is fine — see `registerInstance`).
   */
  async function saveWechatConfig(): Promise<void> {
    wechatConfigSaving.value = true;
    try {
      const id = await resolveInstanceId();
      if (id === null) return;
      await apiJson("POST", "/api/wechat/config", {
        instance_id: id,
        auto_start: wechatAutoConnect.value,
        kind_specific: {},
      });
      toastSuccess(t("wechat.configSaved", "WeChat configuration saved"));
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : t("wechat.saveConfigFailed", "Failed to save WeChat config"));
    } finally {
      wechatConfigSaving.value = false;
    }
  }

  // ── QR login (V1 state machine, V2 REST fields) ───────────────────────────

  /** Poll the QR challenge status every 2s (V1 parity). */
  function startPolling(): void {
    if (pollHandle !== null) return;
    pollHandle = setInterval(() => {
      void pollChallenge();
    }, 2000);
  }

  /**
   * Poll the instance status every 2s for the SILENT reconnect path
   * (V1 `wechatLogin(false)` → `startWechatPolling` with no QR). Stops as
   * soon as the status settles to connected/error (V1 parity).
   */
  function startStatusPolling(): void {
    if (pollHandle !== null) return;
    pollHandle = setInterval(() => {
      void (async () => {
        await loadWechatStatus();
        if (wechatStatus.value === "connected" || wechatStatus.value === "error") {
          stopPolling();
          stopCountdown();
        }
      })();
    }, 2000);
  }

  async function pollChallenge(): Promise<void> {
    if (challengeId === null || instanceId === null) return;
    try {
      const res = await apiJson<QrChallengeResponse>(
        "GET",
        `/api/wechat/qr/${challengeId}/status`,
        undefined,
        { query: { instance_id: instanceId } },
      );
      if (res.status === "scanned") {
        wechatStatus.value = "scanned";
      } else if (res.status === "confirmed") {
        wechatStatus.value = "connected";
        stopPolling();
        stopCountdown();
        void loadWechatStatus();
      } else if (res.status === "expired") {
        // V1 verified: auto re-issue so the user can keep scanning.
        stopCountdown();
        void requestQr();
      }
    } catch {
      // Transient — keep polling until countdown expiry.
    }
  }

  /** Countdown off the challenge `expires_at`; on 0, re-issue (V1 verified). */
  function startCountdown(expiresAtIso: string): void {
    stopCountdown();
    const tick = (): void => {
      const remainS = Math.max(
        0,
        Math.round((new Date(expiresAtIso).getTime() - Date.now()) / 1000),
      );
      wechatQrCountdown.value = remainS;
      if (remainS <= 0) {
        stopCountdown();
        if (wechatStatus.value === "logging_in" || wechatStatus.value === "scanned") {
          void requestQr();
        }
      }
    };
    tick();
    countdownHandle = setInterval(tick, 1000);
  }

  /** Issue a fresh QR challenge for the resolved instance (V1 force pull). */
  async function requestQr(): Promise<void> {
    if (instanceId === null) return;
    wechatQrLoading.value = true;
    wechatQrUrl.value = null;
    wechatStatus.value = "logging_in";
    try {
      const res = await apiJson<QrChallengeResponse>(
        "POST",
        "/api/wechat/qr/issue",
        { instance_id: instanceId },
      );
      challengeId = res.challenge_id;
      wechatQrUrl.value = buildApiUrl(
        `/api/wechat/qr/${res.challenge_id}/image`,
        { instance_id: instanceId },
      );
      wechatQrLoading.value = false;
      startCountdown(res.expires_at);
      startPolling();
    } catch (e) {
      wechatQrLoading.value = false;
      wechatStatus.value = "error";
      toastError(e instanceof ApiError ? e.message : t("wechat.qrIssueFailed", "Failed to issue QR login"));
    }
  }

  /**
   * Connect WeChat (V1 `wechatLogin(force)`).
   *
   * Mirrors the V1 verified two-mode login:
   *   • `force=false` (default) — prefer a SILENT reconnect using stored
   *     credentials: trigger `/api/wechat/login {force:false}` and poll the
   *     instance status; NO QR is shown (V1 `useWechat.js` only starts the QR
   *     countdown when `force` is true).
   *   • `force=true` — force a fresh QR scan: trigger login then issue a new
   *     QR challenge with the 60s countdown + 2s polling.
   *
   * Either way the single instance is resolved first (the backend does not
   * de-dup registers, so we reuse the stored ULID when it still exists).
   */
  async function wechatLogin(force = false): Promise<void> {
    loading.value = true;
    try {
      const id = await resolveInstanceId();
      if (id === null) {
        wechatStatus.value = "error";
        return;
      }
      // V1 parity: hit the legacy /login surface so the wechatbot adapter
      // reuses stored credentials (silent reconnect) or starts a fresh scan.
      // The adapter mints an SDK-driven challenge and returns its id; the
      // real WeChat QR URL arrives on that challenge via the SDK's
      // on_qr_url callback, so we MUST drive the QR display off this id
      // (NOT a separate /qr/issue challenge, which carries no real URL).
      let loginChallengeId: string | null = null;
      try {
        const loginRes = await apiJson<{ ok?: boolean; challenge_id?: string }>(
          "POST",
          "/api/wechat/login",
          { instance_id: id, force },
        );
        loginChallengeId = loginRes?.challenge_id ?? null;
      } catch (e) {
        // 409 = a login is already in progress — keep going (V1 ignores 409).
        if (!(e instanceof ApiError) || !e.message.includes("409")) {
          // Non-conflict failure on the silent path → surface as error.
          if (!force) {
            wechatStatus.value = "error";
            toastError(e instanceof ApiError ? e.message : t("wechat.loginFailed", "WeChat login failed"));
            return;
          }
        }
      }
      if (force) {
        // Force scan: display the SDK-driven challenge's QR. The image
        // endpoint 404s until the SDK reports a URL, so we poll it via the
        // <img> reload loop; status polling drives scanned/confirmed.
        if (loginChallengeId !== null) {
          driveSdkQr(id, loginChallengeId);
        } else {
          // Fallback: no challenge id returned (older backend) — fall back
          // to the state-machine issue path so the UI still shows something.
          await requestQr();
        }
      } else {
        // Default connect (V1 `wechatLogin(false)`): prefer a silent
        // reconnect from stored credentials, BUT still drive the QR display
        // off the SDK challenge. V1 parity: V1's front-end polls `status`
        // which carries `qr_url`, so even on the non-force path the QR shows
        // up the moment the SDK's `on_qr_url` fires (when stored creds are
        // missing/stale and a scan is actually required). If the silent
        // reconnect succeeds instead, the status flips to `connected` and the
        // `connected` branch replaces the QR section — so driving the QR here
        // is harmless when no scan is needed and essential when one is.
        if (loginChallengeId !== null) {
          driveSdkQr(id, loginChallengeId);
        } else {
          // No SDK challenge (silent path on an older backend): show the
          // "logging in" hint and poll instance status only.
          wechatStatus.value = "logging_in";
          startStatusPolling();
        }
      }
    } finally {
      loading.value = false;
    }
  }

  /**
   * Drive the QR display off the SDK-minted challenge from `/api/wechat/login`.
   *
   * The wechatbot SDK reports the real QR URL asynchronously via its
   * `on_qr_url` callback, so `GET /api/wechat/qr-image` 404s until it
   * arrives. v0.5 parity (`WechatConfigPanel.js:168`): the `<img>` src uses a
   * STABLE cache-buster (v0.5 keyed it on the SDK `qr_url` string) so the
   * browser caches the PNG once it loads and never refetches — avoiding the
   * blank/broken-image flash V2 caused by bumping `t=Date.now()` every 2s and
   * hitting a transient 404. Here the stable key is the `challenge_id` (the
   * qr-image for one challenge never changes). We probe the endpoint until it
   * serves a real PNG, set the stable src ONCE, then stop — status polling on
   * the same challenge surfaces scanned/confirmed/expired.
   */
  function driveSdkQr(id: string, sdkChallengeId: string): void {
    challengeId = sdkChallengeId;
    wechatStatus.value = "logging_in";
    wechatQrLoading.value = false;
    // Stable src: cache-buster keyed on the (immutable) challenge_id, NOT a
    // timestamp. Once the browser successfully loads this URL it caches the
    // PNG; subsequent status changes (scanned/connected) never trigger a
    // refetch, so the QR can't degrade to a broken-image placeholder.
    const stableSrc = buildApiUrl("/api/wechat/qr-image", {
      instance_id: id,
      challenge_id: sdkChallengeId,
    });
    startPolling();
    // Probe the qr-image endpoint until the SDK's on_qr_url has populated the
    // challenge and the endpoint serves a real PNG (it 404s before then).
    // Only set the visible <img> src once a probe succeeds, so the user never
    // sees a broken image. Stop probing on first success or when the QR is no
    // longer being shown.
    stopQrImageReload();
    const probe = (): void => {
      if (wechatStatus.value !== "logging_in") {
        stopQrImageReload();
        return;
      }
      const img = new Image();
      img.onload = (): void => {
        // Real PNG available — pin the stable src once and stop probing.
        if (wechatStatus.value === "logging_in" || wechatStatus.value === "scanned") {
          wechatQrUrl.value = stableSrc;
        }
        stopQrImageReload();
      };
      img.onerror = (): void => {
        // Still 404 (SDK url not ready yet) — leave the loading spinner up;
        // the next interval tick re-probes. Never assign a failing URL to the
        // visible <img>, so no broken-image flash.
      };
      // Cache-bust ONLY the probe (not the visible src) so each retry actually
      // re-requests while the endpoint is still 404ing.
      img.src = `${stableSrc}${stableSrc.includes("?") ? "&" : "?"}_probe=${Date.now()}`;
    };
    probe();
    qrImageHandle = setInterval(probe, 2000);
  }

  /** Disconnect WeChat (V1 `wechatLogout`): tear down the Bot + stop transport, reset UI. */
  async function wechatLogout(): Promise<void> {
    stopPolling();
    stopCountdown();
    challengeId = null;
    wechatQrUrl.value = null;
    wechatStatus.value = "idle";
    if (instanceId !== null) {
      try {
        // V1 parity: /logout destroys the wechatbot Bot AND stops the
        // instance. Plain /stop leaves a residual Bot that corrupts the
        // re-scan state machine.
        await apiJson("POST", "/api/wechat/logout", { instance_id: instanceId });
      } catch {
        // Already logged out / not started — ignore.
      }
    }
  }

  function dispose(): void {
    stopPolling();
    stopCountdown();
  }

  return {
    wechatStatus,
    wechatQrUrl,
    wechatQrLoading,
    wechatQrCountdown,
    wechatAutoConnect,
    wechatConfigSaving,
    loading,
    wechatLogin,
    wechatLogout,
    requestQr,
    loadWechatStatus,
    loadWechatConfig,
    saveWechatConfig,
    dispose,
    /** Exposed for settings panels that need the resolved instance id. */
    getInstanceId: () => instanceId,
    /**
     * Ensure an instance exists (register transparently if needed) and return
     * its id — for settings panels that let the user configure model / proxy
     * before connecting (V1 parity: those controls work pre-connection).
     */
    resolveInstanceId,
  };
}

export type UseWechat = ReturnType<typeof useWechat>;

/**
 * provide/inject key so the Channels view can own a single `useWechat`
 * instance (V2 component-isolation alternative to V1's global refs) and
 * share it with both the card-header badge and the config panel.
 */
export const WechatKey: InjectionKey<UseWechat> = Symbol("qai.useWechat");

/** Provide a shared WeChat instance from a parent (the Channels view). */
export function provideWechat(): UseWechat {
  const api = useWechat();
  provide(WechatKey, api);
  return api;
}

/**
 * Inject the shared WeChat instance, falling back to a standalone one if a
 * parent did not provide it (keeps the panel usable in isolation/tests).
 */
export function useWechatShared(): UseWechat {
  return inject(WechatKey) ?? useWechat();
}
