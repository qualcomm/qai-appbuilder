// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Gallery upload store — shared state for the "Submit to Model Gallery"
 * background upload so it survives the dialog being minimized.
 *
 * The dialog writes phase progress here while an upload runs; a global
 * corner indicator (`GalleryUploadIndicator`, mounted once in `App.vue`)
 * reads it to show progress after the user minimizes the dialog to go do
 * something else. `minimized` is owned here so both the dialog and the
 * indicator agree on which surface is currently showing the upload.
 *
 * Progress is phase-based and indeterminate (NOT a byte percentage):
 * measured reality is that file bytes flush near-instantly and the wait is
 * the upstream dashboard receiving + processing the batch, which exposes no
 * progress signal. `packaging` (reading files from disk) IS genuinely
 * per-file, so we surface `currentFile` / `filesDone` there.
 */
import { defineStore } from "pinia";

export type GalleryUploadPhase = "packaging" | "uploading";

export interface GalleryUploadState {
  /** True while a background upload is in flight (dialog may be closed). */
  active: boolean;
  /** True when the user minimized the dialog; the corner indicator shows. */
  minimized: boolean;
  phase: GalleryUploadPhase;
  currentFile: string | null;
  filesDone: number;
  filesTotal: number;
  /** Model name being submitted, for the indicator label. */
  modelName: string;
  /** Epoch ms when the ``uploading`` phase began (for an elapsed timer);
   *  0 until the POST starts. Lets the UI prove liveness while the upstream
   *  (slow) dashboard processes the batch — the only honest signal we have. */
  uploadingStartedAt: number;
}

function initialState(): GalleryUploadState {
  return {
    active: false,
    minimized: false,
    phase: "packaging",
    currentFile: null,
    filesDone: 0,
    filesTotal: 0,
    modelName: "",
    uploadingStartedAt: 0,
  };
}

export const useGalleryUploadStore = defineStore("galleryUpload", {
  state: (): GalleryUploadState => initialState(),
  actions: {
    start(modelName: string): void {
      this.active = true;
      this.phase = "packaging";
      this.currentFile = null;
      this.filesDone = 0;
      this.filesTotal = 0;
      this.modelName = modelName;
      // Do not force-restore the dialog; keep whatever surface is showing.
    },
    update(p: {
      phase: GalleryUploadPhase;
      currentFile: string | null;
      filesDone: number;
      filesTotal: number;
    }): void {
      // Stamp the uploading-phase start once, on the packaging→uploading edge.
      if (p.phase === "uploading" && this.phase !== "uploading") {
        this.uploadingStartedAt = Date.now();
      }
      this.phase = p.phase;
      this.currentFile = p.currentFile;
      this.filesDone = p.filesDone;
      this.filesTotal = p.filesTotal;
    },
    finish(): void {
      this.active = false;
      this.minimized = false;
      this.currentFile = null;
      this.uploadingStartedAt = 0;
    },
    setMinimized(v: boolean): void {
      this.minimized = v;
    },
  },
});
