// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { computed, ref } from "vue";

const STORAGE_KEY = "qai_vla_simulation_settings";

function normalizeServerUrl(value: string): string | null {
  const trimmed = value.trim();
  if (trimmed === "") return "";

  try {
    const url = new URL(trimmed);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url.toString();
  } catch {
    return null;
  }
}

function readServerUrl(): string {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === null) return "";
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object") return "";
    const value = (parsed as Record<string, unknown>)["serverUrl"];
    return typeof value === "string" ? normalizeServerUrl(value) ?? "" : "";
  } catch {
    return "";
  }
}

const _serverUrl = ref(readServerUrl());

export function useVlaSimulation() {
  function saveServerUrl(value: string): boolean {
    const normalized = normalizeServerUrl(value);
    if (normalized === null) return false;

    _serverUrl.value = normalized;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ serverUrl: normalized }));
    } catch {
      // The in-memory setting remains usable when browser storage is unavailable.
    }
    return true;
  }

  return {
    serverUrl: computed(() => _serverUrl.value),
    saveServerUrl,
  };
}

export { normalizeServerUrl };
