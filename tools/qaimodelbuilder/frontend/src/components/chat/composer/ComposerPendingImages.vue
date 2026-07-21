<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ComposerPendingImages — pending-image thumbnail strip
 * (ARCH-1 cohesion split from `ChatComposer.vue`, zero behaviour change).
 *
 * V1 index.html:1444-1449. Lives INSIDE `.rich-input-box` at the top so the
 * strip shares the input frame's border/background (V1 parity). Class names
 * use V1's flat naming (`.pending-images-bar` / `.pending-image-item` /
 * `.pending-image-thumb` / `.pending-image-remove`) so the global tokens in
 * `chat.css` supply all visuals; only the V1-absent failed-upload visual
 * (uses `--error` token) is kept scoped here.
 */
import { useI18n } from "vue-i18n";
import type { PendingImage } from "@/composables/chat/usePendingImages";

defineProps<{
  pendingImages: PendingImage[];
}>();

const emit = defineEmits<{
  open: [dataUrl: string];
  remove: [id: string];
}>();

const { t } = useI18n();
</script>

<template>
  <div
    v-if="pendingImages.length > 0"
    class="pending-images-bar"
  >
    <div
      v-for="img in pendingImages"
      :key="img.id"
      class="pending-image-item"
      :class="{ 'pending-image-item--failed': img.failed }"
      :title="img.failed ? t('chat.attachUploadFailed') : img.name"
    >
      <img
        :src="img.dataUrl"
        :alt="img.name"
        class="pending-image-thumb"
        @click="emit('open', img.dataUrl)"
      />
      <button
        type="button"
        class="pending-image-remove"
        :aria-label="t('chat.attachImageRemove')"
        :title="t('chat.attachImageRemove')"
        @click="emit('remove', img.id)"
      >
        ✕
      </button>
    </div>
  </div>
</template>

<style scoped>
/* V1-absent failed-upload visual (uses `--error` token, no hardcoded
 * colours). The rest of the pending-image visuals come from the global
 * `chat.css` tokens. */
.pending-image-item--failed .pending-image-thumb {
  border-color: var(--error);
  box-shadow: 0 0 0 1px var(--error) inset;
}
</style>
