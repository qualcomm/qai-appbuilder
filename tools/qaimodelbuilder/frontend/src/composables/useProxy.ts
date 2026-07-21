// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useProxy` — global network-proxy credentials (V1 parity).
 *
 * Wraps the dedicated proxy endpoint (separate from forge-config):
 *
 *   GET  /api/proxy → { proxy_url, proxy_username, proxy_password }
 *   POST /api/proxy   body { proxy_url, proxy_username, proxy_password }
 *
 * Backend behaviour (interfaces/http/routes/user_prefs.py):
 *   - `proxy_url` / `proxy_username` persist to
 *     `forge_config.network_proxy.{proxy_url,proxy_username}`.
 *   - `proxy_password` is stored via SecretStore (AGENTS.md §3.3); the
 *     GET response masks an existing password as `****` and never
 *     returns the plaintext. Sending the mask back is a no-op (keep),
 *     sending an empty string clears it, any other value replaces it.
 *
 * This mirrors V1 `useForgeConfig.{loadGlobalProxy,saveGlobalProxy}`.
 */
import { ref, type Ref } from "vue";
import { useI18n } from "vue-i18n";

import { apiJson } from "@/api";
import { useToastStore } from "@/stores/toast";

// ─── Types ───────────────────────────────────────────────────────────────────

interface ProxyResponse {
  proxy_url: string;
  proxy_username: string;
  proxy_password: string;
}

/** Mask string the backend returns for a set-but-hidden password. */
export const PROXY_PASSWORD_MASK = "****";

// ─── Composable ──────────────────────────────────────────────────────────────

export function useProxy() {
  const proxyUrl: Ref<string> = ref("");
  const proxyUsername: Ref<string> = ref("");
  const proxyPassword: Ref<string> = ref("");
  const showPassword: Ref<boolean> = ref(false);
  const loading: Ref<boolean> = ref(false);
  const saving: Ref<boolean> = ref(false);

  const toast = useToastStore();
  const { t } = useI18n();

  async function loadProxy(): Promise<void> {
    loading.value = true;
    try {
      const res = await apiJson<ProxyResponse>("GET", "/api/proxy");
      proxyUrl.value = res.proxy_url ?? "";
      proxyUsername.value = res.proxy_username ?? "";
      // The backend masks an existing password; keep the mask so the
      // user sees that a password is set without exposing the secret.
      proxyPassword.value = res.proxy_password ?? "";
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: `${t("forgeConfig.proxyLoadFailed")}: ${(e as Error).message}`,
        timeoutMs: 5000,
      });
    } finally {
      loading.value = false;
    }
  }

  async function saveProxy(): Promise<void> {
    saving.value = true;
    try {
      await apiJson<{ success: boolean }>("POST", "/api/proxy", {
        proxy_url: proxyUrl.value.trim(),
        proxy_username: proxyUsername.value.trim(),
        // Send the password as-is: an unchanged mask is a no-op on the
        // backend, "" clears the secret, any other value replaces it.
        proxy_password: proxyPassword.value,
      });
      // Re-load so the password field reflects the masked/cleared state.
      await loadProxy();
      toast.push({
        id: crypto.randomUUID(),
        kind: "success",
        message: t("forgeConfig.proxySaved"),
        timeoutMs: 3000,
      });
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: `${t("forgeConfig.proxySaveFailed")}: ${(e as Error).message}`,
        timeoutMs: 5000,
      });
    } finally {
      saving.value = false;
    }
  }

  return {
    proxyUrl,
    proxyUsername,
    proxyPassword,
    showPassword,
    loading,
    saving,
    loadProxy,
    saveProxy,
  };
}
