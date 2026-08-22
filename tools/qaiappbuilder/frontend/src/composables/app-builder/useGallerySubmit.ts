// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useGallerySubmit — manages the "Submit to Model Gallery" workflow:
 * scanning workspace files, populating a form, and submitting the
 * package to the gallery dashboard API.
 */

import { ref, computed, watch, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import {
  scanGalleryFiles,
  submitToGallery,
  getGallerySubmitProgress,
  type GalleryFileInfo,
  type GalleryScanResponseDTO,
  type GallerySubmitResponseDTO,
} from "@/api/gallerySubmit";
import { ApiError } from "@/api";
import { useGalleryUploadStore } from "@/stores/galleryUpload";

const STORAGE_KEY_SUBMITTER = "gallery_submitter";
const STORAGE_KEY_EMAIL = "gallery_email";
const STORAGE_KEY_HISTORY = "gallery_submit_history";
const HISTORY_MAX = 50;

/** One persisted gallery submission, shown in the dialog's history list. */
export interface GallerySubmissionRecord {
  readonly uploadId: string;
  readonly modelName: string;
  readonly submitter: string;
  readonly statusUrl: string;
  readonly submittedAt: number;
}

function loadHistory(): GallerySubmissionRecord[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY_HISTORY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as GallerySubmissionRecord[]) : [];
  } catch {
    return [];
  }
}

export function useGallerySubmit(workdir: Ref<string>) {
  const { t, locale } = useI18n();
  const uploadStore = useGalleryUploadStore();

  // ── State ──────────────────────────────────────────────────────────
  const scanning = ref(false);
  const submitting = ref(false);
  const scanResult = ref<GalleryScanResponseDTO | null>(null);
  const submitResult = ref<GallerySubmitResponseDTO | null>(null);
  const error = ref<string>("");
  const history = ref<GallerySubmissionRecord[]>(loadHistory());

  /** Prepend a successful submission to the persisted history list. */
  function recordHistory(result: GallerySubmitResponseDTO): void {
    const rec: GallerySubmissionRecord = {
      uploadId: result.upload_id,
      modelName: modelName.value,
      submitter: submitter.value,
      statusUrl: result.status_url,
      submittedAt: Date.now(),
    };
    history.value = [rec, ...history.value].slice(0, HISTORY_MAX);
    localStorage.setItem(STORAGE_KEY_HISTORY, JSON.stringify(history.value));
  }

  // ── Form fields (pre-filled from scan, user-editable) ─────────────
  const submitter = ref(localStorage.getItem(STORAGE_KEY_SUBMITTER) || "");
  const email = ref(localStorage.getItem(STORAGE_KEY_EMAIL) || "");
  const modelName = ref("");
  const category = ref<"genai" | "non_genai">("non_genai");
  const modelType = ref("");
  const customer = ref("");
  const customCustomer = ref("");
  const qnnVersion = ref("");
  const quantMethod = ref("");
  const scenario = ref("");
  const notebookUrl = ref("");
  const description = ref("");

  // ── File selection ─────────────────────────────────────────────────
  const files = ref<(GalleryFileInfo & { checked: boolean })[]>([]);

  // ── Derived ────────────────────────────────────────────────────────
  const hasRequiredDlc = computed(() =>
    files.value.some(
      (f) => f.checked && (f.type === "dlc" || f.type === "model" || (f.filename ?? f.path ?? "").toLowerCase().endsWith(".dlc")),
    ),
  );

  const canSubmit = computed(
    () =>
      !!submitter.value &&
      !!email.value &&
      !!modelName.value &&
      hasRequiredDlc.value &&
      !submitting.value,
  );

  const selectedFiles = computed(() =>
    files.value.filter((f) => f.checked).map((f) => f.path),
  );

  const hasModelCard = computed(() =>
    files.value.some((f) => (f.filename ?? "").toUpperCase() === "MODEL_CARD.MD"),
  );

  // ── Actions ────────────────────────────────────────────────────────

  /** Scan workspace and populate form fields from the API response. */
  async function scan(): Promise<void> {
    if (!workdir.value) return;
    scanning.value = true;
    error.value = "";
    try {
      const result = await scanGalleryFiles(workdir.value);
      scanResult.value = result;

      // Map file list with default selection (all checked)
      files.value = result.files.map((f) => ({ ...f, checked: true }));

      // Pre-fill form fields from scan metadata when available
      if (result.model_name) modelName.value = result.model_name;
      if (result.qnn_version) qnnVersion.value = result.qnn_version;
      if (result.quant_method) quantMethod.value = result.quant_method;
      if (result.category) category.value = result.category;
    } catch (e) {
      if (e instanceof ApiError) {
        error.value = e.message;
      } else {
        error.value = e instanceof Error ? e.message : String(e);
      }
    } finally {
      scanning.value = false;
    }
  }

  /** Validate fields, persist user identity to localStorage, and submit. */
  async function submit(): Promise<void> {
    error.value = "";

    if (!canSubmit.value) {
      error.value = t("modelBuilder.gallery.validationError");
      return;
    }

    // Persist submitter identity for next session
    localStorage.setItem(STORAGE_KEY_SUBMITTER, submitter.value);
    localStorage.setItem(STORAGE_KEY_EMAIL, email.value);

    submitting.value = true;
    uploadStore.start(modelName.value);
    try {
      const accepted = await submitToGallery({
        submitter: submitter.value,
        email: email.value,
        model_name: modelName.value,
        model_category: category.value,
        model_type: category.value === "genai" ? modelType.value || undefined : undefined,
        customer: customer.value === "Other" ? customCustomer.value || undefined : customer.value || undefined,
        qnn_version: qnnVersion.value || undefined,
        quant_method: quantMethod.value || undefined,
        scenario: scenario.value || undefined,
        notebook_url: notebookUrl.value || undefined,
        description: description.value || undefined,
        file_paths: selectedFiles.value,
      });

      // Robustness: an OLD synchronous backend returns the final gallery
      // result directly (carries ``status_url``) instead of an ``upload_id``
      // to poll. Detect that and record history immediately — otherwise the
      // poll below hits a 404 (no such progress id) and history never writes.
      const maybeFinal = accepted as unknown as Partial<GallerySubmitResponseDTO>;
      if (typeof maybeFinal.status_url === "string") {
        submitResult.value = maybeFinal as GallerySubmitResponseDTO;
        recordHistory(maybeFinal as GallerySubmitResponseDTO);
        return;
      }

      // Poll phase progress until the background upload settles.
      for (;;) {
        const p = await getGallerySubmitProgress(accepted.upload_id);
        if (p.status === "done") {
          submitResult.value = p.result;
          if (p.result) recordHistory(p.result);
          break;
        }
        if (p.status === "error") {
          error.value = p.error_message || t("modelBuilder.gallery.submitFailed", { reason: "upload error" });
          break;
        }
        uploadStore.update({
          phase: p.phase === "uploading" ? "uploading" : "packaging",
          currentFile: p.current_file,
          filesDone: p.files_done,
          filesTotal: p.files_total,
        });
        const { promise, resolve } = Promise.withResolvers<void>();
        setTimeout(resolve, 400);
        await promise;
      }
    } catch (e) {
      if (e instanceof ApiError) {
        error.value = e.message;
      } else {
        error.value = e instanceof Error ? e.message : String(e);
      }
    } finally {
      submitting.value = false;
      uploadStore.finish();
    }
  }

  /** Clear all state back to initial values. */
  function reset(): void {
    scanning.value = false;
    submitting.value = false;
    scanResult.value = null;
    submitResult.value = null;
    error.value = "";
    uploadStore.finish();
    modelName.value = "";
    category.value = "non_genai";
    modelType.value = "";
    customer.value = "";
    customCustomer.value = "";
    qnnVersion.value = "";
    quantMethod.value = "";
    scenario.value = "";
    notebookUrl.value = "";
    description.value = "";
    files.value = [];
    // Note: submitter/email are intentionally preserved (localStorage identity)
  }

  /** Re-read persisted history from localStorage (call on dialog open so a
   *  record written by a previous submit — or another instance — shows up). */
  function reloadHistory(): void {
    history.value = loadHistory();
  }

  // ── MODEL_CARD.md generation prompt ────────────────────────────────
  const modelCardPrompt = computed(() => {
    if (locale.value.startsWith("zh")) {
      return `请根据本次模型转换的完整过程，在工作区 ${workdir.value} 中生成 MODEL_CARD.md 文件，包含：\n1. 模型基本信息（名称、来源、用途、输入输出）\n2. 转换流程摘要（导出 → 转换 → 量化 → 验证各步关键参数）\n3. 遇到的问题与解决方案（算子 patch、精度调优、特殊处理）\n4. 最终精度与性能数据\n5. 复现建议（环境、QNN 版本、注意事项）`;
    }
    return `Based on the full model conversion process in this session, generate a MODEL_CARD.md file in the workspace ${workdir.value}, including:\n1. Model information (name, source, purpose, inputs/outputs)\n2. Conversion summary (export → convert → quantize → validate, key parameters at each step)\n3. Problems encountered and solutions (operator patches, accuracy tuning, special handling)\n4. Final accuracy and performance data\n5. Reproduction tips (environment, QNN version, caveats)`;
  });

  // ── Auto-scan when workdir changes ─────────────────────────────────
  watch(workdir, (v) => { if (v) scan(); }, { immediate: true });

  return {
    // State
    scanning,
    submitting,
    scanResult,
    submitResult,
    error,
    uploadStore,
    history,
    reloadHistory,
    // Form fields
    submitter,
    email,
    modelName,
    category,
    modelType,
    customer,
    customCustomer,
    qnnVersion,
    quantMethod,
    scenario,
    notebookUrl,
    description,
    // File selection
    files,
    // Derived
    hasRequiredDlc,
    canSubmit,
    selectedFiles,
    hasModelCard,
    modelCardPrompt,
    // Actions
    scan,
    submit,
    reset,
  };
}
