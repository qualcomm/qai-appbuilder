// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Model Catalog Pinia store.
 *
 * S5 PR-055: wraps /api/model-catalog/* routes via apiJson.
 * Exposes: entries list, download start/cancel, providers.
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import { apiJson } from "@/api";
import type { components } from "@/types/api";

type ModelEntryResponse = components["schemas"]["ModelEntryResponse"];
type ModelEntriesResponse = components["schemas"]["ModelEntriesResponse"];
type DownloadJobResponse = components["schemas"]["DownloadJobResponse"];
type DownloadJobsResponse = components["schemas"]["DownloadJobsResponse"];

export const useModelCatalogStore = defineStore("modelCatalog", () => {
  // ─── State ─────────────────────────────────────────────────────────────────
  const entries = ref<ModelEntryResponse[]>([]);
  const downloadJobs = ref<DownloadJobResponse[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);

  // ─── Actions ───────────────────────────────────────────────────────────────
  async function fetchEntries(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const res = await apiJson<ModelEntriesResponse>("GET", "/api/model-catalog/entries");
      entries.value = res.entries;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  async function fetchDownloadJobs(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const res = await apiJson<DownloadJobsResponse>("GET", "/api/model-catalog/download-jobs");
      downloadJobs.value = res.jobs;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  async function startDownload(
    modelId: string,
    versionId: string,
    targetFilename: string,
  ): Promise<DownloadJobResponse | null> {
    error.value = null;
    try {
      const res = await apiJson<DownloadJobResponse>("POST", "/api/model-catalog/download", {
        model_id: modelId,
        version_id: versionId,
        target_filename: targetFilename,
      });
      downloadJobs.value = [...downloadJobs.value, res];
      return res;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
      return null;
    }
  }

  async function cancelDownload(jobId: string): Promise<void> {
    error.value = null;
    try {
      await apiJson("DELETE", `/api/model-catalog/download/${jobId}`);
      downloadJobs.value = downloadJobs.value.filter((j) => j.job_id !== jobId);
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  return {
    entries,
    downloadJobs,
    loading,
    error,
    fetchEntries,
    fetchDownloadJobs,
    startDownload,
    cancelDownload,
  };
});
