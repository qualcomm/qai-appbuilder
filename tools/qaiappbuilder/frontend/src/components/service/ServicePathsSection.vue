<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ServicePathsSection — developer-only local path overrides of the Service
 * page, placed right below the connection bar.
 *
 * Hidden unless the backend reports `enabled` (the API process was started
 * with `QAI_LOCAL_MODEL_DEBUG` set); the same switch makes the backend honour
 * the two paths, so a hidden section can never leave a stale override active.
 *
 * Two rows, both rendered by `ServicePathRow` and both saved as soon as the
 * path is committed (blur / Enter; no save button):
 *   - GenieAPIService directory → `GET/POST /api/service/install-dir`
 *     (owned here; emits `install-dir-saved` so the page re-probes);
 *   - model config file `<model root>\<model name>\config.json` (the
 *     service `-c`) → parsed here into `{ modelRoot, modelName }` and emitted
 *     as `save-model-config`; the page persists `models_root_path` and selects
 *     the model (empty value = default `data/models`).
 */
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { fetchInstallDir, saveInstallDir } from "@/api/serviceControl";
import { ApiError } from "@/api/errors";
import { useToastStore } from "@/stores/toast";

import ServicePathRow from "./ServicePathRow.vue";

const props = defineProps<{
  /** Effective exe path from the status probe ("" when not found). */
  exePath: string;
  modelsRootPath: string;
  /** `config_path` of the selected model as scanned by the backend. */
  selectedModelConfigPath: string;
}>();

const emit = defineEmits<{
  (e: "install-dir-saved"): void;
  (e: "save-model-config", value: { modelRoot: string; modelName: string }): void;
}>();

const { t } = useI18n();
const toast = useToastStore();

const enabled = ref(false);
const installDir = ref("");
const modelConfigPath = ref("");

watch(
  () => [props.modelsRootPath, props.selectedModelConfigPath],
  () => {
    modelConfigPath.value = props.modelsRootPath ? props.selectedModelConfigPath : "";
  },
  { immediate: true },
);

const installDirPlaceholder = computed(() => {
  const p = props.exePath;
  if (!p) return t("service.installDirAuto");
  const idx = Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"));
  return idx > 0 ? p.slice(0, idx) : p;
});

function notify(kind: "success" | "error", message: string): void {
  toast.push({ id: crypto.randomUUID(), kind, message, timeoutMs: kind === "error" ? 5000 : 3000 });
}

async function loadInstallDir(): Promise<void> {
  try {
    const res = await fetchInstallDir();
    enabled.value = res.enabled === true;
    installDir.value = res.path ?? "";
  } catch {
    // graceful — stay hidden when the backend cannot say
  }
}

// NOTE: the internal build offers a native "Browse…" picker per row via the
// shared `POST /api/workspace/browse-files|browse-folder` dialog
// (`@/api/workspacePaths`). That API does not exist in this tree, so the
// paths are typed/pasted instead (saved on blur / Enter).

async function saveInstallDirValue(value: string): Promise<string | null> {
  try {
    const res = await saveInstallDir(value);
    notify("success", t("service.installDirSaved"));
    emit("install-dir-saved");
    return res.path ?? "";
  } catch (e) {
    if (e instanceof ApiError && e.status === 422) {
      notify("error", t("service.installDirInvalid"));
    } else {
      notify("error", `${t("service.saveFailed")}: ${(e as Error).message}`);
    }
    return null;
  }
}

async function saveModelConfigValue(raw: string): Promise<string | null> {
  if (!raw) {
    emit("save-model-config", { modelRoot: "", modelName: "" });
    return "";
  }
  const parts = raw.split(/[/\\]+/);
  const fileName = (parts.pop() ?? "").toLowerCase();
  const modelName = parts.pop() ?? "";
  const sep = raw.includes("\\") ? "\\" : "/";
  const modelRoot = parts.join(sep);
  if (
    (fileName !== "config.json" && fileName !== "genie_config.json") ||
    !modelName ||
    !modelRoot
  ) {
    notify("error", t("service.modelConfigInvalid"));
    return null;
  }
  emit("save-model-config", { modelRoot, modelName });
  return raw;
}

onMounted(() => {
  void loadInstallDir();
});
</script>

<template>
  <div
    v-if="enabled"
    class="service-params-section service-paths-section"
  >
    <div class="paths-title">📁 {{ t("service.pathsTitle") }}</div>

    <ServicePathRow
      v-model="installDir"
      :label="t('service.installDir')"
      :placeholder="installDirPlaceholder"
      :save="saveInstallDirValue"
    />

    <ServicePathRow
      v-model="modelConfigPath"
      :label="t('service.modelConfig')"
      :placeholder="t('service.modelConfigDefault')"
      :save="saveModelConfigValue"
    />
  </div>
</template>

<style scoped>
.service-paths-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.paths-title {
  font-weight: 600;
  font-size: var(--text-sm);
}
</style>
