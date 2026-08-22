// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useFeishu` — single-instance Feishu channel (V1-aligned).
 *
 * UI/flow mirrors the V1 verified single-instance experience
 * (`channels/feishu/useFeishu.js`): the user fills in the Feishu app
 * credentials (App ID / App Secret / Encrypt Key / Verification Token)
 * and clicks "Connect Feishu". There is NO instance list / register form
 * — Feishu is a single instance and has NO QR scan (pure App-credential +
 * webhook/WebSocket mode).
 *
 * State machine (照搬 V1 `useFeishu.js`):
 *   stopped → starting → running | error
 *   startFeishu: set starting → POST start → immediately load status once →
 *     if not running/error, start 2s polling.
 *   stopFeishu: POST stop → stopped, stop polling.
 *   2s polling stops as soon as status hits running/error.
 *   loadFeishuStatus: if backend reports `starting` and no poll is active,
 *     start polling (handles autoStart-on-boot entering the page).
 *
 * The V2 backend is internally multi-instance (every instance is a
 * registered ULID; there is NO `default`, NO "list by kind" endpoint, and
 * register does NOT de-dup). To present the V1 single-instance experience
 * we keep the instance_id transparent:
 *   • persist the instance_id in localStorage (guarded with try/catch)
 *   • resolve: reuse the stored id if `GET /api/feishu/status` still finds
 *     it (200); otherwise `POST /api/feishu/register` exactly once and
 *     store the returned ULID.
 *
 * Backend contract (TestClient-verified, interfaces/http/routes/channels.py):
 *   POST /api/feishu/register {name,secret_service,secret_key,secret_value,metadata}
 *        → { instance_id(ULID), kind, name, status, last_error, ... }
 *        For Feishu the route forces the credential into the single
 *        namespace (FEISHU_APP_SECRET_SERVICE, instance_id) holding the
 *        BARE app_secret — the SAME record the outbound token cache, the
 *        inbound WebSocket transport, and POST /api/feishu/config all
 *        read/write.  The front-end no longer packs "app_id:app_secret"
 *        or writes a placeholder (that diverged from the read paths and
 *        broke real sends).
 *   GET  /api/feishu/status?instance_id=  → { instance{status,last_error}, health }
 *        instance.status ∈ stopped|starting|running|stopping|error.
 *   POST /api/feishu/start  body { instance_id }
 *   POST /api/feishu/stop   body { instance_id }
 *   GET  /api/feishu/config?instance_id=  → { auto_start, kind_specific, has_app_secret }
 *   POST /api/feishu/config body { instance_id, auto_start, kind_specific, app_secret }
 *        non-secret fields (app_id/encrypt_key/verification_token) go to
 *        kind_specific; the plaintext app_secret rides the dedicated
 *        ``app_secret`` field and is written by the route layer to the
 *        SecretStore (AGENTS.md §3.3) — empty ``app_secret`` = preserve
 *        (the saved value / has_app_secret flag is left untouched, so
 *        the form can show a "(saved)" placeholder without re-entry).
 */
import { inject, provide, reactive, ref, type InjectionKey } from "vue";
import { useI18n } from "vue-i18n";

import { apiJson, ApiError } from "@/api";
import { useToastStore } from "@/stores/toast";

// ─── Types ───────────────────────────────────────────────────────────────────

/** V1-aligned channel status (`useFeishu.js:14`). */
export type FeishuStatus = "stopped" | "starting" | "running" | "error";

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

interface ConfigResponse {
  auto_start: boolean;
  kind_specific: Record<string, string>;
  has_app_secret?: boolean;
}

const LS_KEY = "qai.feishu.instance_id";

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

export function useFeishu() {
  const feishuStatus = ref<FeishuStatus>("stopped");
  const feishuError = ref("");
  const feishuConfig = reactive({
    appId: "",
    appSecret: "",
    encryptKey: "",
    verificationToken: "",
    autoStart: false,
    /** Presence-of-saved-app_secret flag (from GET /config) so the form
     * can render a "(saved)" placeholder without echoing the value. */
    hasAppSecret: false,
  });
  const loading = ref(false);
  const saving = ref(false);
  const polling = ref(false);

  let instanceId: string | null = loadStoredInstanceId();
  let pollHandle: ReturnType<typeof setInterval> | null = null;

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
      await apiJson<StatusResponse>("GET", "/api/feishu/status", undefined, {
        query: { instance_id: id },
      });
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Register the single Feishu instance. The Feishu route forces the
   * credential into the single ``(FEISHU_APP_SECRET_SERVICE,
   * instance_id)`` namespace (bare app_secret), so the front-end no
   * longer packs ``app_id:app_secret`` or sends a "placeholder" — the
   * app_secret is provisioned via ``POST /api/feishu/config``. We pass
   * the current app_secret (usually empty at this point) so a user who
   * types the secret then clicks Connect still gets it stored; an empty
   * value is fine (the instance can be configured incrementally, V1
   * parity).
   */
  async function registerInstance(): Promise<string | null> {
    try {
      const res = await apiJson<ChannelInstanceResponse>(
        "POST",
        "/api/feishu/register",
        {
          name: "Feishu",
          // secret_service / secret_key are ignored by the Feishu route
          // (it forces FEISHU_APP_SECRET_SERVICE + instance_id); sent
          // only to satisfy the shared request schema.
          secret_service: "feishu",
          secret_key: "feishu",
          secret_value: feishuConfig.appSecret,
          metadata: {},
        },
      );
      return res.instance_id;
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : t("feishu.registerFailed", "Failed to register Feishu"));
      return null;
    }
  }

  /**
   * Resolve the single Feishu instance id, reusing the stored one if it
   * still exists, else registering exactly one (never blindly re-register
   * — the backend does not de-dup).
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

  // ── Status polling (V1 startFeishuPolling / stopFeishuPolling) ────────────

  function stopPolling(): void {
    if (pollHandle !== null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
    polling.value = false;
  }

  /** Poll the channel status every 2s; stop on running/error (V1 parity). */
  function startPolling(): void {
    if (pollHandle !== null) return;
    polling.value = true;
    pollHandle = setInterval(() => {
      void (async () => {
        await loadFeishuStatus();
        if (feishuStatus.value === "running" || feishuStatus.value === "error") {
          stopPolling();
        }
      })();
    }, 2000);
  }

  /** Map the backend instance.status to the V1 4-state status. */
  function mapStatus(instStatus: string): FeishuStatus {
    if (instStatus === "running") return "running";
    if (instStatus === "error") return "error";
    if (instStatus === "starting" || instStatus === "stopping") return "starting";
    return "stopped";
  }

  /** Load the channel connection status (V1 `loadFeishuStatus`). */
  async function loadFeishuStatus(): Promise<void> {
    if (instanceId === null) {
      const stored = loadStoredInstanceId();
      if (stored === null) return; // never connected → stay stopped
      instanceId = stored;
    }
    try {
      const res = await apiJson<StatusResponse>(
        "GET",
        "/api/feishu/status",
        undefined,
        { query: { instance_id: instanceId } },
      );
      feishuStatus.value = mapStatus(res.instance.status);
      feishuError.value = res.instance.last_error ?? "";
      // V1 fix: if the backend is still connecting (starting) and we have no
      // active poll (e.g. autoStart-on-boot, user just opened the page),
      // start polling so the UI does not stay stuck on "connecting".
      if (feishuStatus.value === "starting" && pollHandle === null) {
        startPolling();
      }
    } catch {
      // 404 = instance gone (e.g. data reset) → treat as stopped.
      feishuStatus.value = "stopped";
      feishuError.value = "";
    }
  }

  // ── Config (V1 loadFeishuConfig / saveFeishuConfig) ───────────────────────

  /** Load the channel config and back-fill the form (V1 `loadFeishuConfig`). */
  async function loadFeishuConfig(): Promise<void> {
    if (instanceId === null) {
      const stored = loadStoredInstanceId();
      if (stored === null) return; // not registered yet → keep defaults
      instanceId = stored;
    }
    try {
      const res = await apiJson<ConfigResponse>(
        "GET",
        "/api/feishu/config",
        undefined,
        { query: { instance_id: instanceId } },
      );
      const ks = res.kind_specific ?? {};
      feishuConfig.appId = ks.app_id ?? "";
      feishuConfig.encryptKey = ks.encrypt_key ?? "";
      feishuConfig.verificationToken = ks.verification_token ?? "";
      feishuConfig.autoStart = !!res.auto_start;
      feishuConfig.hasAppSecret = !!res.has_app_secret;
      // app_secret is a credential — never echoed back from config; the
      // form shows a "(saved)" placeholder when hasAppSecret is true.
      feishuConfig.appSecret = "";
    } catch {
      // Unregistered instance → keep defaults (graceful, V1 parity).
    }
  }

  /**
   * Save the channel config (V1 `saveFeishuConfig`). Non-secret fields go to
   * kind_specific; the plaintext App Secret rides the dedicated
   * ``app_secret`` field which the route layer writes to the SecretStore
   * (AGENTS.md §3.3). An empty App Secret = "do not change" (the saved
   * value / has_app_secret flag is preserved, so the user can edit other
   * fields without re-entering the secret).
   */
  async function saveFeishuConfig(): Promise<void> {
    saving.value = true;
    try {
      const id = await resolveInstanceId();
      if (id === null) return;
      const appSecret = feishuConfig.appSecret;
      await apiJson("POST", "/api/feishu/config", {
        instance_id: id,
        auto_start: feishuConfig.autoStart,
        kind_specific: {
          app_id: feishuConfig.appId.trim(),
          encrypt_key: feishuConfig.encryptKey,
          verification_token: feishuConfig.verificationToken,
        },
        // Empty = preserve existing secret (don't overwrite with blank).
        app_secret: appSecret,
      });
      // Reflect the just-saved secret as "saved" and drop the plaintext
      // from memory (symmetric with the masked-input convention).
      if (appSecret !== "") {
        feishuConfig.hasAppSecret = true;
        feishuConfig.appSecret = "";
      }
      toastSuccess(t("feishu.configSaved", "Feishu configuration saved"));
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : t("feishu.saveConfigFailed", "Failed to save Feishu config"));
    } finally {
      saving.value = false;
    }
  }

  // ── Channel control (V1 startFeishu / stopFeishu) ─────────────────────────

  /**
   * Connect Feishu (V1 `startFeishu`): set starting → POST start →
   * immediately re-check status; if not running/error, start 2s polling.
   * When the instance is in `error` state, acknowledge first so the domain
   * state machine allows the restart.
   */
  async function startFeishu(): Promise<void> {
    loading.value = true;
    try {
      const id = await resolveInstanceId();
      if (id === null) {
        feishuStatus.value = "error";
        return;
      }
      // If currently in error state, acknowledge first (error → stopped)
      // before attempting to start; the domain rejects start from error.
      // If acknowledge fails it has already surfaced its own (accurate) toast
      // and re-thrown — return here so we don't fall into the outer catch and
      // emit a second, misleading "Start failed" toast.
      if (feishuStatus.value === "error") {
        try {
          await acknowledgeFeishu();
        } catch {
          // acknowledgeFeishu already toasted the real reason and left the
          // status as "error"; nothing more to report.
          return;
        }
      }
      feishuStatus.value = "starting";
      feishuError.value = "";
      await apiJson("POST", "/api/feishu/start", { instance_id: id });
      // V1 fix: immediately query status so a synchronously-connected backend
      // does not leave the UI stuck on "connecting".
      await loadFeishuStatus();
      // `loadFeishuStatus` may have mutated the ref; read it freshly (cast
      // breaks TS control-flow narrowing from the "starting" assignment above).
      const settled = feishuStatus.value as FeishuStatus;
      if (settled !== "running" && settled !== "error") {
        startPolling();
      }
    } catch (e) {
      feishuStatus.value = "error";
      feishuError.value = e instanceof ApiError ? e.message : t("feishu.startFailedFallback", "Start failed");
      toastError(e instanceof ApiError ? e.message : t("feishu.startFailed", "Feishu channel start failed"));
    } finally {
      loading.value = false;
    }
  }

  /**
   * Acknowledge the error state (error → stopped) so the instance can be
   * restarted. Called automatically by stopFeishu / startFeishu when the
   * current status is `error`.
   */
  async function acknowledgeFeishu(): Promise<void> {
    if (instanceId === null) return;
    try {
      await apiJson("POST", "/api/feishu/acknowledge", { instance_id: instanceId });
      feishuStatus.value = "stopped";
      feishuError.value = "";
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : t("feishu.acknowledgeFailed", "Failed to acknowledge Feishu error"));
      throw e;
    }
  }

  /** Disconnect Feishu (V1 `stopFeishu`): POST stop → stopped, stop polling.
   * When the instance is in `error` state, acknowledge first (error → stopped)
   * instead of calling stop (which would be rejected by the state machine).
   */
  async function stopFeishu(): Promise<void> {
    loading.value = true;
    try {
      if (instanceId !== null) {
        if (feishuStatus.value === "error") {
          // error state: acknowledge clears it to stopped (stop would be rejected).
          await acknowledgeFeishu();
        } else {
          await apiJson("POST", "/api/feishu/stop", { instance_id: instanceId });
        }
      }
      feishuStatus.value = "stopped";
      feishuError.value = "";
      stopPolling();
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : t("feishu.stopChannelFailed", "Failed to stop Feishu"));
    } finally {
      loading.value = false;
    }
  }

  function dispose(): void {
    stopPolling();
  }

  return {
    feishuStatus,
    feishuError,
    feishuConfig,
    loading,
    saving,
    polling,
    loadFeishuStatus,
    loadFeishuConfig,
    saveFeishuConfig,
    acknowledgeFeishu,
    startFeishu,
    stopFeishu,
    dispose,
    /** Exposed for settings panels that need the resolved instance id. */
    getInstanceId: () => instanceId,
    /**
     * Ensure an instance exists (register transparently if needed) and return
     * its id — for settings panels that let the user configure model / proxy
     * before connecting (V1 parity).
     */
    resolveInstanceId,
  };
}

export type UseFeishu = ReturnType<typeof useFeishu>;

/**
 * provide/inject key so the Channels view can own a single `useFeishu`
 * instance (V2 component-isolation alternative to V1's global refs) and
 * share it with both the card-header badge and the config panel.
 */
export const FeishuKey: InjectionKey<UseFeishu> = Symbol("qai.useFeishu");

/** Provide a shared Feishu instance from a parent (the Channels view). */
export function provideFeishu(): UseFeishu {
  const api = useFeishu();
  provide(FeishuKey, api);
  return api;
}

/**
 * Inject the shared Feishu instance, falling back to a standalone one if a
 * parent did not provide it (keeps the panel usable in isolation/tests).
 */
export function useFeishuShared(): UseFeishu {
  return inject(FeishuKey) ?? useFeishu();
}
