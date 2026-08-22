<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ImageDropzone — App Builder image input (drag / click / paste → upload).
 *
 * V1 parity (`frontend/js/components/app-builder/inputs/ImageDropzone.js`):
 *   - 💾 icon + "Drop, click or paste an image" title + supported-formats hint
 *   - Global Ctrl+V paste of clipboard images (mounted-lifetime listener)
 *   - Real upload via `POST /api/images/upload` (b64 body) — `modelValue`
 *     carries the backend **path** (pipeline reads the file by path); the
 *     thumbnail uses the HTTP **url** kept in a local ref, decoupled from
 *     `modelValue` so a collapsed/re-expanded panel can re-derive its preview.
 *   - Inline red error on format / size-limit violations (never silently drops)
 *   - Change-image / clear actions + path display + uploading state
 *
 * V2 design (better than the V1 monolith): typed `<script setup>`, a single
 * `handleFile` pipeline shared by click / drop / paste, small pure helpers
 * (`validateFile`, `readAsBase64`), reuse of the shared `apiJson` client and
 * the global `ab-*` style tokens (no per-component CSS drift). `modelValue`
 * stays a `string | null` path to match the parent's `setInputKey('image', …)`
 * contract.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { apiJson, ApiError } from "@/api";
import type { components } from "@/types/api";

type ImageUploadResponse = components["schemas"]["ImageUploadResponse"];

interface Props {
  /** Backend image path (or url) currently selected; `null` when empty. */
  modelValue?: string | null;
  /** `<input accept>` filter. */
  accept?: string;
  /** Maximum file size in megabytes (inline error when exceeded). */
  maxSizeMb?: number;
  /**
   * Allowed format hints (e.g. `["png", "jpg", "webp"]`). When provided, the
   * file extension / MIME suffix is validated against it (V1 `constraints.formats`).
   */
  formats?: readonly string[];
}

const props = withDefaults(defineProps<Props>(), {
  modelValue: null,
  accept: "image/*",
  maxSizeMb: 10,
  formats: () => [],
});

const emit = defineEmits<{
  "update:modelValue": [value: string | null];
}>();

const { t } = useI18n();

const dragOver = ref(false);
const uploading = ref(false);
const errorText = ref("");
/** HTTP url for `<img>`; decoupled from `modelValue` (which is the path). */
const previewUrl = ref<string | null>(null);
const fileInputEl = ref<HTMLInputElement | null>(null);

const currentPath = computed<string>(() => props.modelValue ?? "");
const hasImage = computed<boolean>(() => currentPath.value !== "");

// Derive a preview url from an existing path when (re)mounting with a value
// already set, e.g. after the panel was collapsed and expanded again.
watch(
  currentPath,
  (path) => {
    if (path !== "" && previewUrl.value === null) {
      previewUrl.value = path;
    } else if (path === "") {
      previewUrl.value = null;
    }
  },
  { immediate: true },
);

// ── file picker / drag / paste ────────────────────────────────────────────
function openFilePicker(): void {
  fileInputEl.value?.click();
}

function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (file) void handleFile(file);
  // reset so re-selecting the same file still triggers `change`
  input.value = "";
}

function onDragOver(): void {
  dragOver.value = true;
}

function onDragLeave(): void {
  dragOver.value = false;
}

function onDrop(event: DragEvent): void {
  dragOver.value = false;
  const file = event.dataTransfer?.files?.[0];
  if (file) void handleFile(file);
}

function onPaste(event: ClipboardEvent): void {
  const items = event.clipboardData?.items ?? [];
  for (const item of items) {
    if (item.kind === "file" && item.type.startsWith("image/")) {
      const file = item.getAsFile();
      if (file) {
        event.preventDefault();
        void handleFile(file);
        return;
      }
    }
  }
}

// ── validation + upload ─────────────────────────────────────────────────────
function validateFile(file: File): string | null {
  const formats = props.formats;
  if (formats.length > 0) {
    const ext = (file.name.split(".").pop() ?? "").toLowerCase();
    const mime = file.type.toLowerCase();
    const ok = formats.some((fmt) => {
      const f = String(fmt).toLowerCase().replace(/^\./, "");
      return ext === f || mime.endsWith(`/${f}`);
    });
    if (!ok) {
      return t("appBuilder.imageInput.formatNotAllowed", {
        formats: formats.join("/"),
      });
    }
  }
  if (props.maxSizeMb > 0) {
    const sizeMb = file.size / (1024 * 1024);
    if (sizeMb > props.maxSizeMb) {
      return t("appBuilder.imageInput.fileTooLarge", {
        size: sizeMb.toFixed(1),
        max: props.maxSizeMb,
      });
    }
  }
  return null;
}

function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => {
      const dataUrl = String(reader.result ?? "");
      const idx = dataUrl.indexOf(",");
      resolve(idx >= 0 ? dataUrl.slice(idx + 1) : dataUrl);
    };
    reader.readAsDataURL(file);
  });
}

async function handleFile(file: File): Promise<void> {
  errorText.value = "";
  const validationError = validateFile(file);
  if (validationError !== null) {
    errorText.value = validationError;
    return;
  }
  uploading.value = true;
  try {
    const b64 = await readAsBase64(file);
    const stamp = Date.now();
    const res = await apiJson<ImageUploadResponse>(
      "POST",
      "/api/images/upload",
      {
        conv_id: `appbuilder-${stamp}`,
        msg_id: `img-${stamp}-${Math.random().toString(36).slice(2, 7)}`,
        b64_data: b64,
        mime_type: file.type || "image/png",
      },
    );
    // Thumbnail uses the http url; modelValue stores the path (url fallback).
    previewUrl.value = res.url || null;
    emit("update:modelValue", res.path ?? res.url);
  } catch (err) {
    // V1 parity: surface the upload failure inline (ImageDropzone.js:175).
    errorText.value =
      err instanceof ApiError
        ? err.message
        : t("appBuilder.imageInput.uploadFailed", { err: String(err) });
  } finally {
    uploading.value = false;
  }
}

function clear(): void {
  previewUrl.value = null;
  errorText.value = "";
  emit("update:modelValue", null);
}

// ── global paste (mounted lifetime only) ────────────────────────────────────
onMounted(() => {
  window.addEventListener("paste", onPaste);
});
onBeforeUnmount(() => {
  window.removeEventListener("paste", onPaste);
});
</script>

<template>
  <div class="ab-image-dropzone">
    <!-- Selected image: thumbnail + path + change/clear actions -->
    <div
      v-if="hasImage"
      class="ab-image-preview"
    >
      <img
        v-if="previewUrl"
        :src="previewUrl"
        class="ab-image-thumb"
        :alt="t('appBuilder.aria.imagePreview')"
      />
      <div
        v-else
        class="ab-image-thumb ab-image-thumb-placeholder"
      >
        <span aria-hidden="true">&#128247;</span>
      </div>
      <div class="ab-image-meta">
        <div
          class="ab-image-path"
          :title="currentPath"
        >
          {{ currentPath }}
        </div>
        <div class="ab-image-actions">
          <button
            type="button"
            class="ab-btn ab-btn-secondary"
            :disabled="uploading"
            @click="openFilePicker"
          >
            {{ uploading ? t("appBuilder.uploading") : t("appBuilder.changeImage") }}
          </button>
          <button
            type="button"
            class="ab-btn ab-btn-ghost"
            :disabled="uploading"
            @click="clear"
          >
            {{ t("appBuilder.clear") }}
          </button>
        </div>
      </div>
    </div>

    <!-- Empty state: drag / click / paste drop zone -->
    <div
      v-else
      class="ab-image-zone"
      :class="{ 'is-dragover': dragOver, 'is-uploading': uploading }"
      role="button"
      tabindex="0"
      @click="openFilePicker"
      @keydown.enter="openFilePicker"
      @keydown.space.prevent="openFilePicker"
      @dragover.prevent="onDragOver"
      @dragleave="onDragLeave"
      @drop.prevent="onDrop"
    >
      <div
        class="ab-image-zone-icon"
        aria-hidden="true"
      >
        &#128190;
      </div>
      <div class="ab-image-zone-title">
        {{ uploading ? t("appBuilder.uploading") : t("appBuilder.imageDropzoneTitle") }}
      </div>
      <div class="ab-image-zone-hint">
        {{ t("appBuilder.imageDropzoneHint") }}
      </div>
    </div>

    <!-- Inline error (never a native alert) -->
    <div
      v-if="errorText"
      class="ab-input-error"
      role="alert"
    >
      <span aria-hidden="true">&#9888;</span> {{ errorText }}
    </div>

    <!-- Hidden file input -->
    <input
      ref="fileInputEl"
      type="file"
      :accept="accept"
      style="display: none"
      @change="onFileChange"
    />
  </div>
</template>
