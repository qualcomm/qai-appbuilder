// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useOcService — V1-parity OpenCode service-control composable.
 *
 * Extracted from `OpenCodeConfigPanel.vue` (cohesion split). Owns the
 * OpenCode serve-process lifecycle (start / stop / status / uptime), the
 * health badge, and the OPENCODE_PASSWORD credential (load / save / clear
 * via the SecretStore-backed credential endpoints — never config).
 *
 * Injected deps: `pushToast` (host toast helper). No watch, no lifecycle
 * hooks: pure ref + computed + async fetches. The host wires
 * `onMounted(() => { void loadCredentials(); void checkHealth(); void
 * loadProcStatus(); })` and calls `savePassword()` inside its `saveConfig`.
 */
import { computed, ref, type ComputedRef, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import {
  fetchOcHealth,
  fetchOcServiceStatus,
  startOcService,
  stopOcService,
  saveOcCredentials,
  deleteOcCredential,
  fetchOcCredentials,
} from "@/api";
import type { OcServiceStatusResponse } from "@/types/aiCoding";
import type { CodingHealthResponse } from "@/api/aiCodingHealth";

type ToastKind = "success" | "error" | "info";

export interface UseOcServiceReturn {
  health: Ref<CodingHealthResponse | null>;
  healthLoading: Ref<boolean>;
  procStatus: Ref<OcServiceStatusResponse | null>;
  procLoading: Ref<boolean>;
  uptimeText: ComputedRef<string>;
  passwordInput: Ref<string>;
  passwordConfigured: Ref<boolean>;
  showPassword: Ref<boolean>;
  checkHealth: () => Promise<void>;
  loadProcStatus: () => Promise<void>;
  loadCredentials: () => Promise<void>;
  onStart: () => Promise<void>;
  onStop: () => Promise<void>;
  onRefresh: () => Promise<void>;
  /** Persist the password to the SecretStore if changed (called by host saveConfig). */
  savePassword: () => Promise<void>;
  clearPassword: () => Promise<void>;
}

export function useOcService(opts: {
  /** Whether the OC integration is enabled (gates the Start button). */
  enabled: () => boolean;
  /** Host toast helper. */
  pushToast: (kind: ToastKind, message: string) => void;
}): UseOcServiceReturn {
  const { t } = useI18n();
  const { enabled, pushToast } = opts;

  const health = ref<CodingHealthResponse | null>(null);
  const healthLoading = ref(false);
  const procStatus = ref<OcServiceStatusResponse | null>(null);
  const procLoading = ref(false);

  // Password is credential material — never persisted in config.
  const passwordInput = ref("");
  const passwordConfigured = ref(false);
  const showPassword = ref(false);

  const uptimeText = computed(() => {
    const s = procStatus.value?.uptime_seconds;
    if (!s) return "";
    if (s < 60) return `${Math.round(s)}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  });

  async function checkHealth(): Promise<void> {
    healthLoading.value = true;
    try {
      health.value = await fetchOcHealth();
    } catch {
      health.value = null;
    } finally {
      healthLoading.value = false;
    }
  }

  async function loadProcStatus(): Promise<void> {
    try {
      procStatus.value = await fetchOcServiceStatus();
    } catch {
      procStatus.value = null;
    }
  }

  async function loadCredentials(): Promise<void> {
    try {
      const res = await fetchOcCredentials();
      passwordConfigured.value = res.credentials.OPENCODE_PASSWORD?.configured ?? false;
      if (passwordConfigured.value) passwordInput.value = "****";
    } catch { /* non-fatal */ }
  }

  // ─── Service control ─────────────────────────────────────────────────────────
  async function onStart(): Promise<void> {
    if (!enabled()) return;
    procLoading.value = true;
    try {
      const res = await startOcService();
      if (res.already_running) pushToast("info", t("aiCoding.config.serviceAlreadyRunning", "Service already running"));
      else pushToast("success", t("aiCoding.config.serviceStarted", "Service started") + ` (PID ${res.pid ?? "?"})`);
      await loadProcStatus();
      setTimeout(() => void checkHealth(), 2000);
    } catch (e) {
      pushToast("error", t("aiCoding.config.startFailed", "Start failed: ") + (e as Error).message);
    } finally {
      procLoading.value = false;
    }
  }

  async function onStop(): Promise<void> {
    procLoading.value = true;
    try {
      await stopOcService(false);
      pushToast("success", t("aiCoding.config.serviceStopped", "Service stopped"));
      await loadProcStatus();
      await checkHealth();
    } catch (e) {
      pushToast("error", t("aiCoding.config.stopFailed", "Stop failed: ") + (e as Error).message);
    } finally {
      procLoading.value = false;
    }
  }

  async function onRefresh(): Promise<void> {
    await Promise.all([checkHealth(), loadProcStatus()]);
  }

  // ─── Password credential (SecretStore) ───────────────────────────────────────
  async function savePassword(): Promise<void> {
    // Only if changed; "****" is masked-skip (V1 parity).
    if (passwordInput.value && passwordInput.value !== "****") {
      const res = await saveOcCredentials({ OPENCODE_PASSWORD: passwordInput.value });
      if (res.saved.includes("OPENCODE_PASSWORD")) {
        passwordConfigured.value = true;
        passwordInput.value = "****";
      }
    }
  }

  async function clearPassword(): Promise<void> {
    try {
      await deleteOcCredential("OPENCODE_PASSWORD");
      passwordConfigured.value = false;
      passwordInput.value = "";
      pushToast("info", t("aiCoding.config.passwordCleared", "Password cleared"));
    } catch (e) {
      pushToast("error", t("aiCoding.config.deleteFailed", "Delete failed: ") + (e as Error).message);
    }
  }

  return {
    health,
    healthLoading,
    procStatus,
    procLoading,
    uptimeText,
    passwordInput,
    passwordConfigured,
    showPassword,
    checkHealth,
    loadProcStatus,
    loadCredentials,
    onStart,
    onStop,
    onRefresh,
    savePassword,
    clearPassword,
  };
}
