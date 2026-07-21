// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useConfig` — general application configuration.
 *
 * Fetches and saves global app config via the forge-config endpoint.
 * The backend stores a nested config dict; we unwrap/wrap on fetch/save.
 *
 * Endpoints:
 *   GET  /api/forge-config   → { config: {...} }
 *   POST /api/forge-config   → { config: {...} }
 */
import { ref, type Ref } from "vue";
import { useI18n } from "vue-i18n";

import { apiJson } from "@/api";
import { useToastStore } from "@/stores/toast";

// ─── Types ───────────────────────────────────────────────────────────────────

export interface AppConfig {
  [key: string]: unknown;
}

interface ForgeConfigResponse {
  config: Record<string, unknown>;
}

// ─── Composable ──────────────────────────────────────────────────────────────

export function useConfig() {
  const config: Ref<AppConfig | null> = ref(null);
  const loading: Ref<boolean> = ref(false);

  const toast = useToastStore();
  const { t } = useI18n();

  async function fetchConfig(): Promise<void> {
    loading.value = true;
    try {
      const res = await apiJson<ForgeConfigResponse>("GET", "/api/forge-config");
      config.value = res.config as AppConfig;
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: `${t("config.loadFailed")}: ${(e as Error).message}`,
        timeoutMs: 5000,
      });
    } finally {
      loading.value = false;
    }
  }

  async function saveConfig(partial: Partial<AppConfig>): Promise<void> {
    try {
      const res = await apiJson<ForgeConfigResponse>("POST", "/api/forge-config", { config: partial });
      config.value = res.config as AppConfig;
      toast.push({
        id: crypto.randomUUID(),
        kind: "success",
        message: t("config.saved"),
        timeoutMs: 3000,
      });
    } catch (e) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: `${t("config.saveFailed")}: ${(e as Error).message}`,
        timeoutMs: 5000,
      });
    }
  }

  return {
    config,
    loading,
    fetchConfig,
    saveConfig,
  };
}
