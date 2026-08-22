// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useUploadElapsed — a live-ticking "elapsed since uploading began" label
 * for the gallery upload progress surfaces (dialog inline bar + corner
 * indicator).
 *
 * Why this exists: the upstream Model Gallery dashboard is very slow
 * (measured ~0.05–0.1 MB/s) and exposes NO receive/processing progress, so
 * the `uploading` phase is genuinely indeterminate. A static "waiting for
 * server" label reads as "frozen / dead" after a minute. A ticking elapsed
 * timer is the one honest liveness signal we can show, so the user knows the
 * upload is still alive and how long it has taken.
 *
 * The ticker is cleaned up on unmount, so idle dialogs cost nothing.
 */
import { computed, onUnmounted, ref, type ComputedRef } from "vue";
import { useGalleryUploadStore } from "@/stores/galleryUpload";

export interface UploadElapsed {
  /** Reactive `mm:ss` string; empty while not in the uploading phase. */
  elapsedText: ComputedRef<string>;
}

export function useUploadElapsed(): UploadElapsed {
  const uploadStore = useGalleryUploadStore();
  const now = ref(Date.now());

  const timer = window.setInterval(() => {
    now.value = Date.now();
  }, 1000);
  onUnmounted(() => window.clearInterval(timer));

  const elapsedText = computed<string>(() => {
    if (!uploadStore.uploadingStartedAt) return "";
    const secs = Math.max(0, Math.floor((now.value - uploadStore.uploadingStartedAt) / 1000));
    const mm = Math.floor(secs / 60);
    const ss = secs % 60;
    return `${mm}:${ss.toString().padStart(2, "0")}`;
  });

  return { elapsedText };
}
