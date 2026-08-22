<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * GallerySubmitDialog — Submit to Model Gallery workflow.
 *
 * Displays as a full-screen overlay dialog with a form for metadata,
 * file selection, and one-click submission to the gallery dashboard API.
 */
import { computed, onMounted, onUnmounted, ref, toRef, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useAuthStore } from "@/stores/auth";
import { useConfirm } from "@/composables/useConfirm";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useGallerySubmit } from "@/composables/app-builder/useGallerySubmit";
import { useUploadElapsed } from "@/composables/app-builder/useUploadElapsed";
import {
  openFileInSystem,
  deleteFileFromSystem,
  generateGalleryDescription,
} from "@/api/gallerySubmit";

interface Props {
  /** Absolute path to the session model working directory. */
  sessionModelWorkdir: string;
  /** Whether the dialog is open. */
  open: boolean;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  close: [];
  "generate-model-card": [prompt: string];
}>();

const { t } = useI18n();
const { confirm } = useConfirm();
const chatTabs = useChatTabsStore();

const workdirRef = toRef(props, "sessionModelWorkdir");

const {
  scanning,
  submitting,
  scanResult,
  submitResult,
  error,
  uploadStore,
  history,
  reloadHistory,
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
  files,
  hasRequiredDlc,
  canSubmit,
  hasModelCard,
  modelCardPrompt,
  scan,
  submit,
  reset,
} = useGallerySubmit(workdirRef);

// ── Auth: auto-fill submitter & email from login ──────────────────────────
const authStore = useAuthStore();

/** Trigger scan whenever dialog opens; also pre-fill user fields. */
watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) {
      reloadHistory();
      scan();
      if (!submitter.value && authStore.user) {
        submitter.value = authStore.user.display_name || authStore.user.name || authStore.user.username || "";
      }
      if (!email.value && authStore.user?.email) {
        email.value = authStore.user.email;
      }
    }
  },
  { immediate: true },
);

// ── ESC to close ─────────────────────────────────────────────────────────
function onKeydown(e: KeyboardEvent) {
  if (e.key === "Escape" && props.open) {
    e.stopPropagation();
    handleClose();
  }
}
onMounted(() => document.addEventListener("keydown", onKeydown));
onUnmounted(() => document.removeEventListener("keydown", onKeydown));

function handleClose() {
  emit("close");
}

function handleMinimize() {
  uploadStore.setMinimized(true);
}

const { elapsedText } = useUploadElapsed();

function handleGenerateModelCard() {
  emit("generate-model-card", modelCardPrompt.value);
}

// ── Description generation (direct cloud-model call) ───────────────────────
const generatingDesc = ref(false);
const descError = ref("");

async function handleGenerateDescription() {
  if (generatingDesc.value) return;
  generatingDesc.value = true;
  descError.value = "";
  try {
    const modelId = chatTabs.activeTab?.modelId || undefined;
    const res = await generateGalleryDescription(props.sessionModelWorkdir, modelId);
    description.value = res.description;
  } catch (e) {
    descError.value = e instanceof Error ? e.message : String(e);
  } finally {
    generatingDesc.value = false;
  }
}

async function handleSubmit() {
  await submit();
}

// ── File filter ───────────────────────────────────────────────────────────
const fileFilter = ref("");

// ── File grouping by type ─────────────────────────────────────────────────
interface FileGroup {
  type: string;
  label: string;
  files: typeof files.value;
}

const TYPE_ORDER = ["dlc", "bin", "py", "md"] as const;
const TYPE_LABELS: Record<string, string> = {
  dlc: "Model (DLC)",
  bin: "Binary (BIN)",
  py: "Script (PY)",
  md: "Document (MD)",
};

const filteredFiles = computed(() => {
  const q = fileFilter.value.toLowerCase().trim();
  if (!q) return files.value;
  return files.value.filter((f) => {
    const name = (f.filename || f.path || "").toLowerCase();
    return name.includes(q);
  });
});

const fileGroups = computed<FileGroup[]>(() => {
  const groups: FileGroup[] = [];
  const byType = new Map<string, typeof files.value>();
  for (const f of filteredFiles.value) {
    const ext = getFileExt(f.filename || f.path || "");
    const arr = byType.get(ext) || [];
    arr.push(f);
    byType.set(ext, arr);
  }
  // Ordered types first, then remaining
  for (const tp of TYPE_ORDER) {
    const arr = byType.get(tp);
    if (arr && arr.length > 0) {
      groups.push({ type: tp, label: TYPE_LABELS[tp] || tp.toUpperCase(), files: arr });
      byType.delete(tp);
    }
  }
  for (const [tp, arr] of byType) {
    if (arr.length > 0) {
      groups.push({ type: tp, label: tp.toUpperCase(), files: arr });
    }
  }
  return groups;
});

// ── Selected files summary ────────────────────────────────────────────────
const selectedSummary = computed(() => {
  const selected = files.value.filter((f) => f.checked);
  const totalBytes = selected.reduce((sum, f) => sum + f.size, 0);
  return { count: selected.length, size: formatSize(totalBytes) };
});

// ── Submit button tooltip (why disabled) ──────────────────────────────────
const submitDisabledReason = computed<string>(() => {
  if (!submitter.value) return t("modelBuilder.gallery.missingSubmitter");
  if (!email.value) return t("modelBuilder.gallery.missingEmail");
  if (!modelName.value) return t("modelBuilder.gallery.missingModelName");
  if (!hasRequiredDlc.value) return t("modelBuilder.gallery.noDlcWarning");
  return "";
});

// ── Open file ─────────────────────────────────────────────────────────────
const VIEWABLE_EXTS = new Set(["py", "md", "txt", "json", "yaml", "yml", "toml", "cfg", "log", "csv"]);

function isViewable(filename: string): boolean {
  const ext = getFileExt(filename);
  return VIEWABLE_EXTS.has(ext);
}

async function openFile(filePath: string) {
  try {
    await openFileInSystem(filePath);
  } catch {
    // Silent fail — file may not exist or system doesn't support
  }
}

// ── Group collapse/expand ──────────────────────────────────────────────────
const collapsedGroups = ref<Set<string>>(new Set());

function toggleGroup(type: string) {
  const next = new Set(collapsedGroups.value);
  if (next.has(type)) next.delete(type);
  else next.add(type);
  collapsedGroups.value = next;
}

function isCollapsed(type: string): boolean {
  return collapsedGroups.value.has(type);
}

// ── Per-group select / deselect ─────────────────────────────────────────────
function setGroupChecked(group: FileGroup, checked: boolean) {
  for (const f of group.files) f.checked = checked;
}

function groupAllChecked(group: FileGroup): boolean {
  return group.files.length > 0 && group.files.every((f) => f.checked);
}

// ── Delete file (with confirm) ──────────────────────────────────────────────
async function handleDeleteFile(file: { filename?: string; path?: string; rel_path?: string }) {
  const name = getFileName(file);
  const ok = await confirm({
    title: t("modelBuilder.gallery.deleteFile"),
    message: t("modelBuilder.gallery.confirmDelete", { name, path: file.path ?? name }),
    confirmText: t("modelBuilder.gallery.deleteFile"),
    cancelText: t("modelBuilder.gallery.cancel"),
    confirmStyle: "danger",
  });
  if (!ok || !file.path) return;
  try {
    await deleteFileFromSystem(file.path);
    files.value = files.value.filter((f) => f.path !== file.path);
  } catch {
    // Silent fail — file may already be gone; a rescan would reconcile
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────
function getFileExt(filename: string): string {
  const name = filename.split(/[/\\]/).pop() || "";
  return (name.split(".").pop() || "").toLowerCase();
}

function getFileName(file: { filename?: string; path?: string }): string {
  return file.filename || (file.path || "").split(/[/\\]/).pop() || "";
}

/**
 * Relative subdirectory of a file within the workdir (POSIX), WITHOUT the
 * filename — e.g. ``artifacts/int8`` for ``artifacts/int8/model.dlc``.
 * Empty string for top-level files. Lets the UI disambiguate same-named
 * files living in different subdirectories.
 */
function getRelDir(file: { rel_path?: string }): string {
  const rel = file.rel_path ?? "";
  const idx = rel.lastIndexOf("/");
  return idx > 0 ? rel.slice(0, idx) : "";
}

const CUSTOMERS = [
  "Ali",
  "Honor",
  "OPPO",
  "Vivo",
  "Xiaomi",
  "Tencent",
  "Baidu",
  "Lenovo",
  "NIO",
  "OpenSource",
  "Other",
] as const;

const MODEL_TYPES = ["LLM", "VLM", "LVM", "Omni", "Embedding", "MoE"] as const;

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

// ── Submission history helpers ─────────────────────────────────────────────
const GALLERY_BASE_URL = "http://modelgallery.qualcomm.com:3000";

function formatSubmittedAt(ts: number): string {
  return new Date(ts).toLocaleString();
}

function statusHref(statusUrl: string): string {
  return /^https?:\/\//.test(statusUrl) ? statusUrl : GALLERY_BASE_URL + statusUrl;
}

// ── Type badge colors ─────────────────────────────────────────────────────
function badgeClass(filename: string): string {
  const ext = getFileExt(filename);
  return `gallery-dialog__file-badge--${ext}`;
}
</script>

<template>
  <Teleport to="body">
    <Transition name="gallery-dialog-fade">
      <div
        v-if="open && !uploadStore.minimized"
        class="gallery-dialog-overlay"
        @click.self="handleClose"
      >
        <div
          class="gallery-dialog"
          role="dialog"
          aria-modal="true"
        >
          <!-- Header (sticky) -->
          <header class="gallery-dialog__header">
            <span class="gallery-dialog__title">{{ t("modelBuilder.gallery.title") }}</span>
            <a
              class="gallery-dialog__gallery-link"
              href="https://modelgallery.qualcomm.com/"
              target="_blank"
              rel="noopener noreferrer"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" /><polyline points="15 3 21 3 21 9" /><line x1="10" y1="14" x2="21" y2="3" /></svg>
              {{ t("modelBuilder.gallery.openGallery") }}
            </a>
            <button
              v-if="uploadStore.active"
              class="gallery-dialog__minimize"
              :title="t('modelBuilder.gallery.minimize')"
              @click="handleMinimize"
            >
              &#x2013;
            </button>
            <button
              class="gallery-dialog__close"
              @click="handleClose"
              aria-label="Close"
            >
              &#x2715;
            </button>
          </header>

          <!-- Scrollable body -->
          <div class="gallery-dialog__body">
          <div class="gallery-dialog__tip">
            <span>{{ t("modelBuilder.gallery.tip") }}</span>
            <button
              class="gallery-dialog__btn gallery-dialog__btn--secondary"
              :disabled="hasModelCard"
              @click="handleGenerateModelCard"
            >
              {{ hasModelCard ? t("modelBuilder.gallery.modelCardExists") : t("modelBuilder.gallery.generateModelCard") }}
            </button>
          </div>

          <!-- Submission history (persisted; shown in ALL states so a user
               can look up past submissions even with no active workspace) -->
          <details
            v-if="history.length > 0"
            class="gallery-dialog__history"
          >
            <summary class="gallery-dialog__history-summary">
              {{ t("modelBuilder.gallery.historyTitle", { n: history.length }) }}
            </summary>
            <ul class="gallery-dialog__history-list">
              <li
                v-for="rec in history"
                :key="rec.uploadId + rec.submittedAt"
                class="gallery-dialog__history-item"
              >
                <div class="gallery-dialog__history-main">
                  <span class="gallery-dialog__history-name">{{ rec.modelName || rec.uploadId }}</span>
                  <span class="gallery-dialog__history-time">{{ formatSubmittedAt(rec.submittedAt) }}</span>
                </div>
                <div class="gallery-dialog__history-meta">
                  <span class="gallery-dialog__history-id">ID: {{ rec.uploadId }}</span>
                  <a
                    v-if="rec.statusUrl"
                    class="gallery-dialog__history-link"
                    :href="statusHref(rec.statusUrl)"
                    target="_blank"
                    rel="noopener noreferrer"
                  >{{ t("modelBuilder.gallery.checkStatus") }}</a>
                </div>
              </li>
            </ul>
          </details>

          <!-- Loading state -->
          <div
            v-if="scanning"
            class="gallery-dialog__scanning"
          >
            {{ t("modelBuilder.gallery.scanning") }}
          </div>

          <!-- No workspace -->
          <div
            v-else-if="!scanResult"
            class="gallery-dialog__empty"
          >
            {{ t("modelBuilder.gallery.noWorkspace") }}
          </div>

          <!-- Form + files -->
          <template v-else>
            <!-- Error -->
            <div
              v-if="error"
              class="gallery-dialog__error"
            >
              {{ error }}
            </div>

            <!-- Form section -->
            <div class="gallery-dialog__form">
              <!-- Row: submitter + email -->
              <div class="gallery-dialog__form-row">
                <label class="gallery-dialog__field">
                  <span class="gallery-dialog__label">{{ t("modelBuilder.gallery.submitter") }} <span class="gallery-dialog__required">*</span></span>
                  <input
                    v-model="submitter"
                    type="text"
                    class="gallery-dialog__input"
                    required
                  />
                </label>
                <label class="gallery-dialog__field">
                  <span class="gallery-dialog__label">{{ t("modelBuilder.gallery.email") }} <span class="gallery-dialog__required">*</span></span>
                  <input
                    v-model="email"
                    type="email"
                    class="gallery-dialog__input"
                    required
                  />
                </label>
              </div>

              <!-- Row: model_name + category -->
              <div class="gallery-dialog__form-row">
                <label class="gallery-dialog__field">
                  <span class="gallery-dialog__label">{{ t("modelBuilder.gallery.modelName") }} <span class="gallery-dialog__required">*</span></span>
                  <input
                    v-model="modelName"
                    type="text"
                    class="gallery-dialog__input"
                    required
                  />
                </label>
                <fieldset class="gallery-dialog__field gallery-dialog__fieldset">
                  <legend class="gallery-dialog__label">{{ t("modelBuilder.gallery.category") }}</legend>
                  <div class="gallery-dialog__seg">
                    <label
                      class="gallery-dialog__seg-option"
                      :class="{ 'is-active': category === 'genai' }"
                    >
                      <input
                        v-model="category"
                        type="radio"
                        value="genai"
                        class="gallery-dialog__seg-input"
                      />
                      {{ t("modelBuilder.gallery.categoryGenai") }}
                    </label>
                    <label
                      class="gallery-dialog__seg-option"
                      :class="{ 'is-active': category === 'non_genai' }"
                    >
                      <input
                        v-model="category"
                        type="radio"
                        value="non_genai"
                        class="gallery-dialog__seg-input"
                      />
                      {{ t("modelBuilder.gallery.categoryNonGenai") }}
                    </label>
                  </div>
                </fieldset>
              </div>

              <!-- Row: model_type (only when genai) + customer -->
              <div class="gallery-dialog__form-row">
                <label
                  v-if="category === 'genai'"
                  class="gallery-dialog__field"
                >
                  <span class="gallery-dialog__label">{{ t("modelBuilder.gallery.modelType") }}</span>
                  <select
                    v-model="modelType"
                    class="gallery-dialog__select"
                  >
                    <option
                      v-for="mt in MODEL_TYPES"
                      :key="mt"
                      :value="mt"
                    >
                      {{ mt }}
                    </option>
                  </select>
                </label>
                <label class="gallery-dialog__field">
                  <span class="gallery-dialog__label">{{ t("modelBuilder.gallery.customer") }}</span>
                  <select
                    v-model="customer"
                    class="gallery-dialog__select"
                  >
                    <option value="">
                      —
                    </option>
                    <option
                      v-for="c in CUSTOMERS"
                      :key="c"
                      :value="c"
                    >
                      {{ c }}
                    </option>
                  </select>
                </label>
              </div>

              <!-- Custom customer input -->
              <div
                v-if="customer === 'Other'"
                class="gallery-dialog__form-row"
              >
                <label class="gallery-dialog__field">
                  <span class="gallery-dialog__label">{{ t("modelBuilder.gallery.customCustomer") }}</span>
                  <input
                    v-model="customCustomer"
                    type="text"
                    class="gallery-dialog__input"
                  />
                </label>
              </div>

              <!-- Row: qnn_version + quant_method -->
              <div class="gallery-dialog__form-row">
                <label class="gallery-dialog__field">
                  <span class="gallery-dialog__label">{{ t("modelBuilder.gallery.qnnVersion") }}</span>
                  <input
                    v-model="qnnVersion"
                    type="text"
                    class="gallery-dialog__input"
                  />
                </label>
                <label class="gallery-dialog__field">
                  <span class="gallery-dialog__label">{{ t("modelBuilder.gallery.quantMethod") }}</span>
                  <input
                    v-model="quantMethod"
                    type="text"
                    class="gallery-dialog__input"
                  />
                </label>
              </div>

              <!-- Row: scenario + notebook_url -->
              <div class="gallery-dialog__form-row">
                <label class="gallery-dialog__field">
                  <span class="gallery-dialog__label">{{ t("modelBuilder.gallery.scenario") }}</span>
                  <input
                    v-model="scenario"
                    type="text"
                    class="gallery-dialog__input"
                  />
                </label>
                <label class="gallery-dialog__field">
                  <span class="gallery-dialog__label">{{ t("modelBuilder.gallery.notebookUrl") }}</span>
                  <input
                    v-model="notebookUrl"
                    type="url"
                    class="gallery-dialog__input"
                  />
                </label>
              </div>

              <!-- Description -->
              <div class="gallery-dialog__field gallery-dialog__field--full">
                <span class="gallery-dialog__label gallery-dialog__label--with-action">
                  <span>{{ t("modelBuilder.gallery.description") }}</span>
                  <button
                    type="button"
                    class="gallery-dialog__gen-btn"
                    :class="{ 'is-loading': generatingDesc }"
                    :disabled="generatingDesc"
                    :title="t('modelBuilder.gallery.generateDescHint')"
                    @click="handleGenerateDescription"
                  >
                    <span v-if="generatingDesc" class="gallery-dialog__gen-spinner" />
                    {{ generatingDesc ? t("modelBuilder.gallery.generatingDesc") : "\u2728 " + t("modelBuilder.gallery.generateDesc") }}
                  </button>
                </span>
                <textarea
                  v-model="description"
                  class="gallery-dialog__textarea"
                  rows="3"
                />
                <span v-if="descError" class="gallery-dialog__desc-error">{{ descError }}</span>
              </div>
            </div>

            <!-- File list section -->
            <div class="gallery-dialog__files">
              <div class="gallery-dialog__files-header">
                <h4 class="gallery-dialog__files-title">{{ t("modelBuilder.gallery.filesTitle") }}</h4>
                <span v-if="files.length > 0" class="gallery-dialog__files-actions">
                  <button
                    type="button"
                    class="gallery-dialog__link-btn"
                    @click="files.forEach(f => f.checked = true)"
                  >{{ t("modelBuilder.gallery.selectAll") }}</button>
                  <span class="gallery-dialog__files-sep">/</span>
                  <button
                    type="button"
                    class="gallery-dialog__link-btn"
                    @click="files.forEach(f => f.checked = false)"
                  >{{ t("modelBuilder.gallery.deselectAll") }}</button>
                </span>
                <!-- Filter input -->
                <input
                  v-if="files.length > 4"
                  v-model="fileFilter"
                  type="text"
                  class="gallery-dialog__filter-input"
                  :placeholder="t('modelBuilder.gallery.filterPlaceholder')"
                />
              </div>

              <div
                v-if="files.length === 0"
                class="gallery-dialog__empty"
              >
                {{ t("modelBuilder.gallery.noFilesFound") }}
              </div>

              <!-- Grouped file list -->
              <div
                v-else
                class="gallery-dialog__file-list"
              >
                <div
                  v-for="group in fileGroups"
                  :key="group.type"
                  class="gallery-dialog__file-group"
                >
                  <div class="gallery-dialog__group-header">
                    <button
                      type="button"
                      class="gallery-dialog__group-toggle"
                      :aria-expanded="!isCollapsed(group.type)"
                      :title="isCollapsed(group.type) ? t('modelBuilder.gallery.expand') : t('modelBuilder.gallery.collapse')"
                      @click="toggleGroup(group.type)"
                    >
                      <span class="gallery-dialog__group-caret" :class="{ 'is-collapsed': isCollapsed(group.type) }">&#x25BE;</span>
                      <span class="gallery-dialog__group-label">{{ group.label }}</span>
                      <span class="gallery-dialog__group-count">{{ group.files.length }}</span>
                    </button>
                    <label class="gallery-dialog__group-select">
                      <input
                        type="checkbox"
                        class="gallery-dialog__file-check"
                        :checked="groupAllChecked(group)"
                        @change="setGroupChecked(group, ($event.target as HTMLInputElement).checked)"
                      />
                      <span>{{ t("modelBuilder.gallery.selectAll") }}</span>
                    </label>
                  </div>
                  <ul v-show="!isCollapsed(group.type)" class="gallery-dialog__group-list">
                    <li
                      v-for="(file, idx) in group.files"
                      :key="file.path ?? idx"
                      class="gallery-dialog__file-item"
                    >
                      <label class="gallery-dialog__file-label">
                        <input
                          v-model="file.checked"
                          type="checkbox"
                          class="gallery-dialog__file-check"
                        />
                        <span :class="['gallery-dialog__file-badge', badgeClass(getFileName(file))]">
                          {{ getFileExt(getFileName(file)).toUpperCase() }}
                        </span>
                        <span class="gallery-dialog__file-name">{{ getFileName(file) }}</span>
                        <span
                          v-if="getRelDir(file)"
                          class="gallery-dialog__file-reldir"
                          :title="file.path"
                        >{{ getRelDir(file) }}/</span>
                      </label>
                      <button
                        v-if="isViewable(getFileName(file))"
                        type="button"
                        class="gallery-dialog__file-action gallery-dialog__file-open"
                        :title="t('modelBuilder.gallery.openFile')"
                        @click.stop="openFile(file.path)"
                      >
                        &#x1F441;
                      </button>
                      <button
                        type="button"
                        class="gallery-dialog__file-action gallery-dialog__file-delete"
                        :title="t('modelBuilder.gallery.deleteFile')"
                        @click.stop="handleDeleteFile(file)"
                      >
                        &#x1F5D1;
                      </button>
                      <span class="gallery-dialog__file-size">{{ formatSize(file.size) }}</span>
                    </li>
                  </ul>
                </div>
              </div>

              <!-- DLC warning -->
              <div
                v-if="!hasRequiredDlc && files.length > 0"
                class="gallery-dialog__warning"
              >
                {{ t("modelBuilder.gallery.noDlcWarning") }}
              </div>

              <!-- Summary: selected count + total size -->
              <div
                v-if="files.length > 0"
                class="gallery-dialog__files-summary"
              >
                {{ t("modelBuilder.gallery.selectedSummary", { count: selectedSummary.count, size: selectedSummary.size }) }}
              </div>
            </div>

            <!-- Result section (shown after submit) -->
            <div
              v-if="submitResult"
              class="gallery-dialog__result"
            >
              <span class="gallery-dialog__result-msg">
                {{ t("modelBuilder.gallery.submitSuccess", { id: submitResult.upload_id }) }}
              </span>
            </div>

            <!-- Upload progress (phase-based; shown while uploading) -->
            <div
              v-if="uploadStore.active"
              class="gallery-dialog__upload-progress"
            >
              <div class="gallery-dialog__upload-bar gallery-dialog__upload-bar--indeterminate">
                <div class="gallery-dialog__upload-fill" />
              </div>
              <span class="gallery-dialog__upload-label">
                <template v-if="uploadStore.phase === 'packaging'">
                  {{ t("modelBuilder.gallery.packagingFile", { name: uploadStore.currentFile ?? "", done: uploadStore.filesDone, total: uploadStore.filesTotal }) }}
                </template>
                <template v-else>
                  {{ t("modelBuilder.gallery.uploadWaiting") }}<template v-if="elapsedText"> ({{ elapsedText }})</template>
                </template>
              </span>
            </div>

            <!-- Footer -->
            <footer class="gallery-dialog__footer">
              <button
                class="gallery-dialog__btn gallery-dialog__btn--secondary"
                @click="handleClose"
              >
                {{ t("modelBuilder.gallery.cancel") }}
              </button>
              <div
                class="gallery-dialog__submit-wrapper"
                :title="submitDisabledReason"
              >
                <button
                  class="gallery-dialog__btn gallery-dialog__btn--primary"
                  :disabled="!canSubmit"
                  @click="handleSubmit"
                >
                  {{ submitting ? t("modelBuilder.gallery.submitting") : t("modelBuilder.gallery.submit") }}
                </button>
              </div>
            </footer>
          </template>
          </div><!-- /.gallery-dialog__body -->
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.gallery-dialog-overlay {
  position: fixed;
  inset: 0;
  z-index: 9000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(2px);
}

.gallery-dialog {
  display: flex;
  flex-direction: column;
  width: min(720px, 90vw);
  max-height: 85vh;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-primary);
  color: var(--text-primary);
  font-size: var(--text-sm);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
}

/* Header — pinned at top */
.gallery-dialog__header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

/* Scrollable body */
.gallery-dialog__body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.gallery-dialog__title {
  flex: 1;
  font-size: var(--text-lg, 1.125rem);
  font-weight: 600;
}

.gallery-dialog__close {
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--text-md);
  cursor: pointer;
  padding: var(--space-1);
  border-radius: var(--radius-sm);
}

.gallery-dialog__close:hover {
  background: var(--bg-hover, var(--bg-secondary));
  color: var(--text-primary);
}

.gallery-dialog__minimize {
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--text-md);
  line-height: 1;
  cursor: pointer;
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-sm);
}

.gallery-dialog__minimize:hover {
  background: var(--bg-hover, var(--bg-secondary));
  color: var(--text-primary);
}

.gallery-dialog__gallery-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--text-xs);
  color: var(--accent, #a78bfa);
  text-decoration: none;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  transition: background 0.15s;
}

.gallery-dialog__gallery-link:hover {
  background: var(--bg-hover, var(--bg-secondary));
  text-decoration: underline;
}

/* Tip banner */
.gallery-dialog__tip {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  font-size: var(--text-xs);
  color: var(--text-secondary);
}

.gallery-dialog__tip span {
  flex: 1;
}

/* States */
.gallery-dialog__scanning {
  color: var(--text-muted);
  text-align: center;
  padding: var(--space-4);
}

.gallery-dialog__empty {
  color: var(--text-secondary);
  text-align: center;
  padding: var(--space-3);
}

.gallery-dialog__error {
  color: var(--error);
  font-size: var(--text-sm);
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--bg-tertiary, var(--bg-secondary));
}

/* Form grid */
.gallery-dialog__form {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.gallery-dialog__form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
}

.gallery-dialog__field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.gallery-dialog__field--full {
  grid-column: 1 / -1;
}

.gallery-dialog__fieldset {
  border: none;
  margin: 0;
  padding: 0;
}

.gallery-dialog__label {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  font-weight: 500;
}

.gallery-dialog__label--with-action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.gallery-dialog__gen-btn {
  border: 1px solid var(--accent);
  background: transparent;
  color: var(--accent);
  cursor: pointer;
  font-size: 10px;
  font-weight: 600;
  padding: 1px 8px;
  border-radius: var(--radius-full, 9999px);
  transition: background 0.1s, color 0.1s;
}

.gallery-dialog__gen-btn:hover {
  background: var(--accent);
  color: #fff;
}

.gallery-dialog__gen-btn:disabled {
  cursor: default;
  opacity: 0.7;
}

.gallery-dialog__gen-btn:disabled:hover {
  background: transparent;
  color: var(--accent);
}

.gallery-dialog__gen-btn.is-loading {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.gallery-dialog__gen-spinner {
  width: 9px;
  height: 9px;
  border: 1.5px solid var(--accent);
  border-top-color: transparent;
  border-radius: 50%;
  animation: gallery-gen-spin 0.7s linear infinite;
}

@keyframes gallery-gen-spin {
  to { transform: rotate(360deg); }
}

.gallery-dialog__desc-error {
  margin-top: 4px;
  font-size: var(--text-xs);
  color: var(--error, #ef4444);
}

.gallery-dialog__required {
  color: var(--error, #ef4444);
}

.gallery-dialog__input,
.gallery-dialog__select,
.gallery-dialog__textarea {
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-input, var(--bg-secondary));
  color: var(--text-primary);
  font-size: var(--text-sm);
  font-family: inherit;
}

.gallery-dialog__input:focus,
.gallery-dialog__select:focus,
.gallery-dialog__textarea:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent);
}

.gallery-dialog__textarea {
  resize: vertical;
  min-height: 60px;
}

.gallery-dialog__seg {
  display: flex;
  gap: var(--space-2);
  padding-top: var(--space-1);
}

.gallery-dialog__seg-option {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  min-height: 38px;
  padding: var(--space-1) var(--space-2);
  font-size: var(--text-sm);
  line-height: 1.3;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-primary);
  color: var(--text-secondary);
  cursor: pointer;
  transition: border-color 0.12s, background 0.12s, color 0.12s;
}

.gallery-dialog__seg-option:hover {
  border-color: var(--accent);
}

.gallery-dialog__seg-option.is-active {
  border-color: var(--accent);
  background: var(--accent-muted, rgba(124, 92, 246, 0.12));
  color: var(--accent);
  font-weight: 600;
}

/* Native radio input is hidden; the pill itself is the affordance. */
.gallery-dialog__seg-input {
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

/* Files section */
.gallery-dialog__files {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-tertiary, var(--bg-secondary));
}

.gallery-dialog__files-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.gallery-dialog__files-title {
  margin: 0;
  font-size: var(--text-sm);
  font-weight: 600;
}

.gallery-dialog__files-actions {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--text-xs);
}

.gallery-dialog__link-btn {
  background: none;
  border: none;
  color: var(--accent, #a78bfa);
  cursor: pointer;
  padding: 0;
  font-size: inherit;
}

.gallery-dialog__link-btn:hover {
  text-decoration: underline;
}

.gallery-dialog__files-sep {
  color: var(--text-muted);
}

.gallery-dialog__file-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: 420px;
  overflow-y: auto;
}

.gallery-dialog__file-group {
  display: flex;
  flex-direction: column;
}

.gallery-dialog__group-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 2px var(--space-2);
  background: var(--bg-secondary);
  border-radius: var(--radius-sm);
  margin-bottom: 2px;
}

.gallery-dialog__group-toggle {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 1;
  border: none;
  background: transparent;
  cursor: pointer;
  padding: 2px 0;
  color: var(--text-secondary);
}

.gallery-dialog__group-caret {
  font-size: 10px;
  color: var(--text-muted);
  transition: transform 0.15s;
}

.gallery-dialog__group-caret.is-collapsed {
  transform: rotate(-90deg);
}

.gallery-dialog__group-select {
  display: flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  font-size: 10px;
  color: var(--text-muted);
  flex-shrink: 0;
}

.gallery-dialog__group-label {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-secondary);
}

.gallery-dialog__group-count {
  font-size: 10px;
  padding: 0 5px;
  border-radius: var(--radius-full, 9999px);
  background: var(--accent-muted, var(--bg-primary));
  color: var(--accent);
}

.gallery-dialog__group-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}

.gallery-dialog__file-item {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  padding: 3px var(--space-2);
  border-radius: var(--radius-sm);
}

.gallery-dialog__file-item:hover {
  background: var(--bg-hover, var(--bg-primary));
}

.gallery-dialog__file-label {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
  font-size: var(--text-xs);
  flex: 1;
  min-width: 0;
}

.gallery-dialog__file-check {
  accent-color: var(--accent);
  flex-shrink: 0;
}

.gallery-dialog__file-name {
  flex: 0 1 auto;
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.gallery-dialog__file-reldir {
  flex: 1 1 auto;
  min-width: 0;
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  color: var(--text-muted);
  opacity: 0.7;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.gallery-dialog__file-size {
  font-size: 10px;
  color: var(--text-muted);
  white-space: nowrap;
  flex-shrink: 0;
}

.gallery-dialog__file-badge {
  font-size: 10px;
  padding: 0 5px;
  border-radius: var(--radius-full, 9999px);
  font-weight: 600;
  flex-shrink: 0;
  text-transform: uppercase;
}

/* Color-coded type badges */
.gallery-dialog__file-badge--dlc {
  background: rgba(139, 92, 246, 0.15);
  color: #a78bfa;
}
.gallery-dialog__file-badge--bin {
  background: rgba(59, 130, 246, 0.15);
  color: #60a5fa;
}
.gallery-dialog__file-badge--py {
  background: rgba(16, 185, 129, 0.15);
  color: #34d399;
}
.gallery-dialog__file-badge--md {
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
}

.gallery-dialog__file-action {
  border: none;
  background: transparent;
  cursor: pointer;
  padding: 2px 5px;
  font-size: 13px;
  line-height: 1;
  opacity: 0.45;
  transition: opacity 0.1s, background 0.1s;
  border-radius: var(--radius-sm);
  flex-shrink: 0;
  color: var(--text-secondary);
  filter: grayscale(0.2);
}

.gallery-dialog__file-item:hover .gallery-dialog__file-action {
  opacity: 0.85;
}

.gallery-dialog__file-action:hover {
  opacity: 1 !important;
  filter: none;
  background: var(--bg-hover, var(--bg-secondary));
}

.gallery-dialog__file-delete:hover {
  background: rgba(239, 68, 68, 0.15);
}

.gallery-dialog__files-summary {
  font-size: var(--text-xs);
  color: var(--text-muted);
  padding: var(--space-1) var(--space-2);
  text-align: right;
}

.gallery-dialog__filter-input {
  margin-left: auto;
  padding: 2px 8px;
  font-size: var(--text-xs);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-primary);
  color: var(--text-primary);
  width: 140px;
}

.gallery-dialog__filter-input:focus {
  outline: none;
  border-color: var(--accent);
}

.gallery-dialog__submit-wrapper {
  display: inline-block;
}

.gallery-dialog__upload-progress {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) 0 0;
}

.gallery-dialog__upload-bar {
  flex: 1;
  height: 6px;
  border-radius: var(--radius-full, 9999px);
  background: var(--bg-tertiary, var(--bg-secondary));
  overflow: hidden;
}

.gallery-dialog__upload-bar--indeterminate .gallery-dialog__upload-fill {
  position: relative;
  width: 40%;
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
  animation: gallery-upload-indeterminate 1.2s ease-in-out infinite;
}

@keyframes gallery-upload-indeterminate {
  0% { margin-left: -40%; }
  100% { margin-left: 100%; }
}

.gallery-dialog__upload-label {
  flex-shrink: 0;
  font-size: var(--text-xs);
  color: var(--text-secondary);
  min-width: 3.5em;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.gallery-dialog__warning {
  color: var(--warning, #f59e0b);
  font-size: var(--text-xs);
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--bg-primary);
  border: 1px solid var(--warning, #f59e0b);
}

/* Result */
.gallery-dialog__result {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  background: var(--bg-secondary);
  border: 1px solid var(--success, #22c55e);
}

.gallery-dialog__result-msg {
  color: var(--success, #22c55e);
  font-size: var(--text-sm);
}

/* Submission history */
.gallery-dialog__history {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-secondary);
}

.gallery-dialog__history-summary {
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
}

.gallery-dialog__history-list {
  list-style: none;
  margin: 0;
  padding: 0 var(--space-2) var(--space-2);
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 180px;
  overflow-y: auto;
}

.gallery-dialog__history-item {
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--bg-primary);
}

.gallery-dialog__history-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.gallery-dialog__history-name {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.gallery-dialog__history-time {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--text-muted);
}

.gallery-dialog__history-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  margin-top: 2px;
}

.gallery-dialog__history-id {
  font-size: 10px;
  color: var(--text-muted);
  font-family: var(--font-mono, monospace);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.gallery-dialog__history-link {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--accent);
  text-decoration: none;
}

.gallery-dialog__history-link:hover {
  text-decoration: underline;
}

/* Footer */
.gallery-dialog__footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px solid var(--border);
}

/* Buttons */
.gallery-dialog__btn {
  padding: var(--space-1) var(--space-3);
  font-size: var(--text-sm);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-family: inherit;
}

.gallery-dialog__btn--secondary {
  background: var(--bg-tertiary, var(--bg-secondary));
  color: var(--text-primary);
}

.gallery-dialog__btn--secondary:hover:not(:disabled) {
  background: var(--bg-hover, var(--bg-secondary));
}

.gallery-dialog__btn--primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}

.gallery-dialog__btn--primary:hover:not(:disabled) {
  background: var(--accent-hover, var(--accent));
}

.gallery-dialog__btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Transition */
.gallery-dialog-fade-enter-active,
.gallery-dialog-fade-leave-active {
  transition: opacity 0.15s ease;
}

.gallery-dialog-fade-enter-from,
.gallery-dialog-fade-leave-to {
  opacity: 0;
}
</style>
