<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  ModelCatalogTab — V1 "models" tab container (milestone-2: full parity).

  Renders the hardware-grouped model catalog as three collapsible-free
  group sections (NPU → GPU → CPU, V1 order). Each section shows a group
  header (icon + i18n title + count badge) and a stack of `ModelCard`s.

  Owns the wiring between cards and `useModelCatalog` (variant select /
  download / cancel / install / delete / retry / clear-status). The shared
  per-task download state is read from `injectDownloadCtx().downloads`.

  Preserved milestone-1 states: unconfigured / transport-error / empty /
  loading (already V1-aligned — only the "group view" body changed from a
  read-only summary list to live `ModelCard` rendering).

  V1 parity over V1 *structure*: V1 (`DownloadCenterPanel.js:519-1010`)
  copy-pastes the entire group template THREE times (NPU/GPU/CPU). Here a
  single `v-for` over `HARDWARE_ORDER` drives all three groups; the only
  per-group differences (header icon) live in a small lookup map.

  V1 reference:
    DownloadCenterPanel.js:519-528  NPU group header (⚡ + title + badge)
    DownloadCenterPanel.js:754-760  GPU group header (🎮, marginTop when npu present)
    DownloadCenterPanel.js:982-988  CPU group header (🖥️)
-->
<script setup lang="ts">
import { useI18n } from "vue-i18n";

import type {
  CatalogModel,
  DownloadStatus,
  ModelHardware,
} from "@/types/downloads";
import { injectDownloadCtx } from "@/composables/useDownloadCenter";
import { useToastStore } from "@/stores/toast";
import ModelCard from "./ModelCard.vue";

const ctx = injectDownloadCtx();
const { t } = useI18n();
const toast = useToastStore();
const mc = ctx.modelCatalog;

/** V1 render order: NPU → GPU → CPU. */
const HARDWARE_ORDER: readonly ModelHardware[] = ["npu", "gpu", "cpu"];

/** Per-group header icon (V1 panel:525 / 757 / 985). */
const GROUP_ICON: Record<ModelHardware, string> = {
  npu: "⚡",
  gpu: "🎮",
  cpu: "🖥️",
};

function groupTitleKey(hw: ModelHardware): string {
  return `downloads.${hw}ModelsGroup`;
}

/**
 * Per-variant download status map for a model (drives ModelCard's variant
 * selector dots). Maps every `variant_id` → its current shared download
 * status (or undefined when no task exists for that variant yet).
 */
function variantStatusesFor(
  m: CatalogModel,
): Record<string, DownloadStatus | undefined> {
  const result: Record<string, DownloadStatus | undefined> = {};
  for (const v of m.variants) {
    result[v.variant_id] = ctx.downloads.value[v.variant_id]?.status;
  }
  return result;
}

// ─── Card → composable bridge ─────────────────────────────────────────────

async function handleInstall(m: CatalogModel, savePath: string): Promise<void> {
  const res = await mc.install(m, savePath);
  if (res.ok) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "success",
      message: t("downloads.modelInstalledTo", {
        path: res.install_path ?? "",
      }),
      timeoutMs: 4000,
    });
  } else if (res.error !== undefined) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: t("downloads.modelInstallFailed", { msg: res.error }),
      timeoutMs: 5000,
    });
  }
}

async function handleDelete(m: CatalogModel): Promise<void> {
  // A model installs ONE shared copy across its platform variants (mirrors a
  // GenieAPIService version), so delete is keyed by `model_id` — not the
  // selected platform's variant_id. Using the variant_id would 404 ("no model
  // artifacts found") whenever the on-disk dir is the shared `model_id` dir.
  const res = await mc.deleteCatalogModel(m.model_id);
  if (res.ok) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "success",
      message: t("downloads.modelDeletedToast"),
      timeoutMs: 3000,
    });
  } else if (res.error !== undefined) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: t("downloads.deleteFailed", { msg: res.error }),
      timeoutMs: 5000,
    });
  }
}

function handleCancel(m: CatalogModel): void {
  ctx.cancelDownload(mc.taskIdFor(m));
  toast.push({
    id: crypto.randomUUID(),
    kind: "info",
    message: t("downloads.cancelledToast"),
    timeoutMs: 2500,
  });
}
</script>

<template>
  <section
    class="dc-tab dc-tab--models"
    aria-labelledby="dc-models-heading"
  >
    <h2
      id="dc-models-heading"
      class="qai-sr-only"
    >
      {{ t("downloads.tabModels") }}
    </h2>

    <!--
      models-desc info banner — ALWAYS shown at the top of the models tab,
      above every state (V1 DownloadCenterPanel.js:484-489). Explains the
      QNN/GGUF/MNN model formats + driver requirements.
    -->
    <div
      class="dc-tab__desc"
      role="note"
    >
      <span
        class="dc-tab__desc-icon"
        aria-hidden="true"
      >ℹ️</span>
      <span>{{ t("downloads.modelsDesc") }}</span>
    </div>

    <!-- empty state: catalog_url unset (422 unconfigured) -->
    <div
      v-if="mc.loadError.value?.kind === 'unconfigured'"
      class="dc-tab__empty"
      role="status"
    >
      <div
        class="dc-tab__empty-icon"
        aria-hidden="true"
      >
        🧠
      </div>
      <strong>{{ t("downloads.emptyModels") }}</strong>
      <p>{{ t("downloads.emptyModelsHint") }}</p>
      <button
        type="button"
        class="btn btn-primary btn-sm"
        @click="mc.fetchCatalog()"
      >
        {{ t("downloads.loadModelCatalog") }}
      </button>
    </div>

    <!-- transport error -->
    <div
      v-else-if="mc.loadError.value?.kind === 'transport'"
      class="dc-tab__empty dc-tab__empty--error"
      role="alert"
    >
      <div
        class="dc-tab__empty-icon"
        aria-hidden="true"
      >
        🧠
      </div>
      <strong>{{ t("downloads.fetchCatalogFailed") }}</strong>
      <p>{{ mc.loadError.value.message }}</p>
      <button
        type="button"
        class="btn btn-primary btn-sm"
        @click="mc.fetchCatalog()"
      >
        {{ t("downloads.loadModelCatalog") }}
      </button>
    </div>

    <!-- empty list (URL configured but manifest empty) -->
    <div
      v-else-if="!mc.loading.value && mc.modelCount.value === 0"
      class="dc-tab__empty"
      role="status"
    >
      <div
        class="dc-tab__empty-icon"
        aria-hidden="true"
      >
        🧠
      </div>
      <strong>{{ t("downloads.emptyModels") }}</strong>
      <p>{{ t("downloads.emptyModelsHint") }}</p>
      <button
        type="button"
        class="btn btn-primary btn-sm"
        @click="mc.fetchCatalog()"
      >
        {{ t("downloads.loadModelCatalog") }}
      </button>
    </div>

    <!-- loading -->
    <div
      v-else-if="mc.loading.value"
      class="dc-tab__loading"
      role="status"
    >
      <span
        class="dc-tab__spinner"
        aria-hidden="true"
      ></span>
      {{ t("downloads.refreshing") }}
    </div>

    <!-- group view: NPU → GPU → CPU, one ModelCard per model -->
    <div
      v-else
      class="dc-tab__groups"
    >
      <template
        v-for="hw in HARDWARE_ORDER"
        :key="hw"
      >
        <section
          v-if="mc.modelsByHardware.value[hw].length > 0"
          class="dc-group"
          :aria-label="t(groupTitleKey(hw))"
        >
          <div class="dc-group__header">
            <span
              class="dc-group__icon"
              aria-hidden="true"
            >{{
              GROUP_ICON[hw]
            }}</span>
            <span class="dc-group__title">{{ t(groupTitleKey(hw)) }}</span>
            <span class="dc-group__count">{{
              mc.modelsByHardware.value[hw].length
            }}</span>
          </div>

          <div class="dc-group__list">
            <ModelCard
              v-for="m in mc.modelsByHardware.value[hw]"
              :key="m.model_id"
              :model="m"
              :selected-variant-id="mc.effectiveVariantId(m)"
              :task-id="mc.taskIdFor(m)"
              :download-entry="ctx.downloads.value[mc.taskIdFor(m)] ?? null"
              :local-status="mc.localStatusFor(m, mc.taskIdFor(m))"
              :is-any-downloading="ctx.isAnyModelDownloading.value"
              :variant-statuses="variantStatusesFor(m)"
              :install-state="mc.installState.value[m.model_id] ?? null"
              :install-error="mc.installError.value[m.model_id] ?? ''"
              @select-variant="(vid: string) => mc.setSelectedVariant(m.model_id, vid)"
              @start-download="mc.startDownload(m)"
              @cancel="handleCancel(m)"
              @install-to-models="(sp: string) => handleInstall(m, sp)"
              @delete-model="handleDelete(m)"
              @delete-downloaded="handleDelete(m)"
              @retry="mc.startDownload(m)"
              @clear-status="mc.clearStatus(mc.taskIdFor(m))"
            />
          </div>
        </section>
      </template>
    </div>
  </section>
</template>

<style scoped>
/* Shared `.dc-tab__*` banner/empty/loading/spinner styles (deduped with
   ServiceVersionsTab). Tab-specific styles stay below. */
@import "./downloads-tab-shared.css";

.dc-tab {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.dc-tab__groups {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.dc-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* V1 .dc-group-header (downloads.css:403-416): uppercase muted-text label
   with letter-spacing and a 1px bottom divider — reads as a section
   separator rather than a normal heading. */
.dc-group__header {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) 0 6px;
  font-size: var(--text-sm);
  font-weight: 700;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  border-bottom: 1px solid var(--border);
  margin-bottom: var(--space-2);
  flex-shrink: 0;
}

.dc-group__icon {
  font-size: var(--text-md);
  line-height: 1;
}

/* V1 dc-tab-badge style (downloads.css:51-63): accent-light purple pill
   with accent-coloured text — same pill the Tab strip uses. */
.dc-group__count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-left: var(--space-1);
  min-width: 18px;
  height: 18px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--accent-light);
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}

.dc-group__list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.qai-sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
</style>
