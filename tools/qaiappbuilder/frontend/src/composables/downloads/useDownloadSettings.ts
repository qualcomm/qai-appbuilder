// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useDownloadSettings` — read / write the forge_config download section.
 *
 * V1 source-of-truth references:
 *   - DownloadCenterPanel.js:165-234   (the collapsible "Download Settings"
 *                                       region with v-model bindings)
 *   - useDownloadCenter.js:saveDownloadSettings (`saveForgeConfig` →
 *                                       refresh versions+catalog+toast)
 *
 * Wire endpoints:
 *   GET /api/versions/download-settings   → `DownloadSettings`
 *   PUT /api/versions/download-settings   → `DownloadSettings` (echoes
 *                                            persisted, possibly normalised
 *                                            values; sync local state from
 *                                            the response, NOT the request)
 */

import { ref } from "vue";

import {
  fetchDownloadSettings,
  updateDownloadSettings,
} from "@/api/downloads";
import type { DownloadSettings } from "@/types/downloads";

/** Defaults match V1 / backend (`DownloadSettings.to_wire` / `forge_settings.py`). */
function defaultSettings(): DownloadSettings {
  return {
    save_dir: "",
    version_list_url: "",
    catalog_url: "",
    fetch_timeout_seconds: 15,
    download_timeout_seconds: 300,
    ssl_verify: false,
  };
}

export function useDownloadSettings() {
  const settings = ref<DownloadSettings>(defaultSettings());
  const loaded = ref(false);
  const saving = ref(false);
  const error = ref<string | null>(null);

  async function load(): Promise<void> {
    error.value = null;
    try {
      settings.value = await fetchDownloadSettings();
      loaded.value = true;
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
    }
  }

  /**
   * Persist the current `settings` ref via PUT, then sync local state from
   * the server response. Returns `true` on success.
   *
   * The caller is responsible for triggering downstream refreshes (versions
   * list, model catalog) — V1 does that in `saveDownloadSettings` (panel
   * line 30-36). We expose this as a simple boolean result so the caller
   * can decide what to refresh.
   */
  async function save(next?: DownloadSettings): Promise<boolean> {
    saving.value = true;
    error.value = null;
    try {
      const persisted = await updateDownloadSettings(next ?? settings.value);
      settings.value = persisted;
      return true;
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
      return false;
    } finally {
      saving.value = false;
    }
  }

  /**
   * Optimistic local update without persisting (used by v-model on the form
   * inputs; `save()` will then PUT the resulting state).
   */
  function patch(partial: Partial<DownloadSettings>): void {
    settings.value = { ...settings.value, ...partial };
  }

  return {
    settings,
    loaded,
    saving,
    error,
    load,
    save,
    patch,
  };
}

export type UseDownloadSettingsReturn = ReturnType<typeof useDownloadSettings>;
