<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * GalleryUploadIndicator — global corner progress badge for a background
 * Model Gallery upload after the user minimizes the submit dialog.
 *
 * Mounted once in `App.vue`. Reads the shared `galleryUpload` store and shows
 * a compact card ONLY while an upload is active AND minimized. Clicking it
 * restores the dialog (clears `minimized`).
 *
 * Placement: fixed bottom-right. Toasts live top-center and the transient
 * top-right overlays (security/service-log) are modal panels, so the
 * bottom-right corner is free — no overlap with existing widgets.
 *
 * Progress is phase-based/indeterminate (see the store docs): an animated
 * bar plus the current packaging file or an "uploading, waiting for server"
 * label — never a fake byte percentage.
 */
import { useI18n } from "vue-i18n";
import { useGalleryUploadStore } from "@/stores/galleryUpload";
import { useUploadElapsed } from "@/composables/app-builder/useUploadElapsed";

const { t } = useI18n();
const uploadStore = useGalleryUploadStore();
const { elapsedText } = useUploadElapsed();

function restore(): void {
  uploadStore.setMinimized(false);
}
</script>

<template>
  <Teleport to="body">
    <Transition name="gallery-indicator-fade">
      <button
        v-if="uploadStore.active && uploadStore.minimized"
        class="gallery-indicator"
        :title="t('modelBuilder.gallery.restore')"
        @click="restore"
      >
        <div class="gallery-indicator__row">
          <span class="gallery-indicator__title">
            {{ t("modelBuilder.gallery.uploadingModel", { name: uploadStore.modelName }) }}
          </span>
          <span class="gallery-indicator__expand">&#x2197;</span>
        </div>
        <div class="gallery-indicator__bar">
          <div class="gallery-indicator__fill" />
        </div>
        <span class="gallery-indicator__label">
          <template v-if="uploadStore.phase === 'packaging'">
            {{ t("modelBuilder.gallery.packagingFile", { name: uploadStore.currentFile ?? "", done: uploadStore.filesDone, total: uploadStore.filesTotal }) }}
          </template>
          <template v-else>
            {{ t("modelBuilder.gallery.uploadWaiting") }}<template v-if="elapsedText"> ({{ elapsedText }})</template>
          </template>
        </span>
      </button>
    </Transition>
  </Teleport>
</template>

<style scoped>
.gallery-indicator {
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 8500;
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 280px;
  max-width: calc(100vw - 32px);
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 10px);
  background: var(--bg-secondary);
  box-shadow: var(--shadow-md);
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s, transform 0.1s;
}

.gallery-indicator:hover {
  border-color: var(--accent);
  transform: translateY(-1px);
}

.gallery-indicator__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.gallery-indicator__title {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.gallery-indicator__expand {
  flex-shrink: 0;
  color: var(--text-muted);
  font-size: 12px;
}

.gallery-indicator__bar {
  height: 5px;
  border-radius: var(--radius-full, 9999px);
  background: var(--bg-tertiary, var(--bg-primary));
  overflow: hidden;
}

.gallery-indicator__fill {
  width: 40%;
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
  animation: gallery-indicator-indeterminate 1.2s ease-in-out infinite;
}

@keyframes gallery-indicator-indeterminate {
  0% { margin-left: -40%; }
  100% { margin-left: 100%; }
}

.gallery-indicator__label {
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.gallery-indicator-fade-enter-active,
.gallery-indicator-fade-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}

.gallery-indicator-fade-enter-from,
.gallery-indicator-fade-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
</style>
