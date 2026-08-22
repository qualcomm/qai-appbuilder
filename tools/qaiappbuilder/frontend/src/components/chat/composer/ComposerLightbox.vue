<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ComposerLightbox — pending-image preview overlay
 * (ARCH-1 cohesion split from `ChatComposer.vue`, zero behaviour change).
 *
 * V1 index.html:137-148 + app.js:168-213. Opened by clicking a pending-image
 * thumbnail; wheel zooms, drag pans, dblclick resets, click-overlay / Esc
 * closes. Receives the shared `useLightbox` instance from the composer so
 * the keyboard (Esc) lifecycle and `open()` call site stay unchanged.
 *
 * All visuals come from the global `.lightbox-*` tokens (chat.css), so no
 * scoped styles are needed here.
 */
import { useI18n } from "vue-i18n";
import type { useLightbox } from "@/composables/useLightbox";

defineProps<{
  lightbox: ReturnType<typeof useLightbox>;
}>();

const { t } = useI18n();
</script>

<template>
  <div
    v-if="lightbox.isOpen.value"
    class="lightbox-overlay"
    data-testid="composer-image-lightbox"
    role="dialog"
    aria-modal="true"
    @click="lightbox.close"
    @wheel.prevent="lightbox.onWheel"
  >
    <img
      :src="lightbox.src.value ?? ''"
      class="lightbox-image"
      :alt="t('chat.imagePreview', 'image preview')"
      :style="lightbox.imageStyle.value"
      @click.stop
      @mousedown.prevent="lightbox.onDragStart"
      @dblclick="lightbox.reset"
    />
    <button
      type="button"
      class="lightbox-close"
      :aria-label="t('common.close')"
      :title="t('common.close')"
      @click.stop="lightbox.close"
    >
      ✕
    </button>
    <div class="lightbox-hint">
      {{ t("chat.lightboxHint") }}
    </div>
  </div>
</template>
