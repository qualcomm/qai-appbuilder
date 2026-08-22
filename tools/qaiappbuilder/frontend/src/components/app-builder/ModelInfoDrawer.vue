<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModelInfoDrawer — App Builder model details drawer (V1 `ModelInfoDrawer.js`
 * parity).
 *
 * Renders the selected model's display name (+ inline version) + description +
 * runtime / metrics + Variants (status dot · label · DEFAULT · quant · size ·
 * latency · "Not installed") + Tags + Capabilities + Examples (clickable
 * `apply-example`) + Weights URL + Install Path + Delete panel (userImported
 * models only). Uses the global `.ab-info-drawer` / `.ab-drawer-*` /
 * `.model-info-delete*` / `.ab-drawer-delete-*` classes (real design tokens).
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useConfirm } from "@/composables/useConfirm";
import { useEscClose } from "@/composables/useClickOutside";
import type { AppModelCardVM, AppModelVariantVM } from "@/components/app-builder/types";

/**
 * Optional manifest-derived fields the drawer renders when present. These are
 * now populated by `buildModelCardVM` (tags / capabilities / examples /
 * weightsUrl / installPath from the rich manifest). `userImported` is read
 * from the backend DTO's `user_imported` flag (`_dto.py` `AppModelResponse`),
 * which gates the delete panel — only user-imported models are deletable
 * (built-ins are protected server-side with HTTP 403).
 */
interface DrawerModelVM extends AppModelCardVM {
  tags?: readonly string[] | null;
  /** Already-localized capability labels (internal `cancel` filtered upstream). */
  capabilities?: readonly string[] | null;
  examples?: ReadonlyArray<{ name?: string | null; license?: string | null }> | null;
  /** assets.weightsUrl */
  weightsUrl?: string | null;
  /** assets.installPath (single / legacy Pack). */
  installPath?: string | null;
  /** Whether this model was user-imported (not built-in registry). V1 parity: only user-imported models show delete panel. */
  userImported?: boolean;
}

/** Variant fields the drawer renders beyond the base VM, when present. */
interface DrawerVariantVM extends AppModelVariantVM {
  longLabel?: string | null;
  latencyMs?: number | null;
  /** registry status: Ready / NotInstalled / Updating / Downloading / Error */
  status?: string | null;
  /** assets.installPath for this variant. */
  installPath?: string | null;
}

interface Props {
  open: boolean;
  model: AppModelCardVM | null;
  selectedVariantId?: string | null;
}

const props = withDefaults(defineProps<Props>(), { selectedVariantId: null });

const emit = defineEmits<{
  close: [];
  "select-variant": [id: string];
  "apply-example": [example: { name?: string | null; license?: string | null; inputs?: Record<string, unknown>; paramsOverride?: Record<string, unknown> }];
  /** Emitted when user confirms full model delete (single-variant or "delete all"). V1 `emit('deleted', modelId)`. */
  "delete-model": [modelId: string];
  /** Emitted when user confirms partial variant delete (multi-variant picker). V1 `emit('variants-deleted', { modelId, deletedVariants })`. */
  "delete-variants": [modelId: string, variantIds: string[]];
}>();

const { t } = useI18n();

const model = computed<DrawerModelVM | null>(() => props.model as DrawerModelVM | null);

// V1 parity (ModelInfoDrawer.js:128-133): Escape key closes the drawer. The
// `useEscClose` listener stays mounted but `when: () => props.open` short-
// circuits it while the drawer is closed (functionally equivalent to the
// previous bind/unbind dance, with one less moving part).
useEscClose(
  (ev) => {
    ev.preventDefault();
    emit("close");
  },
  () => props.open,
);

const runtimeText = computed<string>(() => {
  const r = model.value?.runtime ?? {};
  const parts: string[] = [];
  if (r.backend) parts.push(String(r.backend).toUpperCase());
  if (r.delegate) parts.push(String(r.delegate).toUpperCase());
  if (r.quantization) parts.push(String(r.quantization).toUpperCase());
  return parts.join(" · ");
});

const latencyText = computed<string | null>(() => {
  const v = Number(model.value?.metrics?.latencyMs);
  return Number.isFinite(v) ? t("appBuilder.variant.latencyHint", { n: Math.round(v) }) : null;
});
const memoryText = computed<string | null>(() => {
  const v = Number(model.value?.metrics?.memoryMB);
  return Number.isFinite(v) ? t("appBuilder.variant.sizeOnDisk", { n: Math.round(v) }) : null;
});

const tags = computed<string[]>(() =>
  Array.isArray(model.value?.tags) ? [...model.value!.tags!] : [],
);
const capabilities = computed<string[]>(() =>
  Array.isArray(model.value?.capabilities) ? [...model.value!.capabilities!] : [],
);
const examples = computed(() =>
  Array.isArray(model.value?.examples) ? model.value!.examples! : [],
);
const weightsUrl = computed<string>(() => model.value?.weightsUrl || "");

/**
 * V1 `variantsList` parity (ModelInfoDrawer.js:55-86). Derives a per-variant
 * status + status dot class from the VM. The VM currently exposes
 * `installed` rather than a full status string — map it to Ready /
 * NotInstalled and prefer an explicit `status` once the VM provides one.
 */
const variantsList = computed(() => {
  const arr = (model.value?.variants ?? []) as DrawerVariantVM[];
  return arr.map((v) => {
    const explicit = typeof v.status === "string" && v.status ? v.status : null;
    const status = explicit ?? (v.installed === false ? "NotInstalled" : "Ready");
    const s = String(status).toLowerCase();
    let statusClass = "status-unknown";
    if (s === "ready") statusClass = "status-ready";
    else if (s === "notinstalled") statusClass = "status-notinstalled";
    else if (s === "updating" || s === "downloading") statusClass = "status-loading";
    else if (s === "error") statusClass = "status-error";
    const sz = Number(v.sizeMB);
    const lat = Number(v.latencyMs);
    return {
      id: v.id,
      label: v.label || v.id,
      longLabel: v.longLabel || "",
      quantization: v.runtime?.quantization || "",
      sizeMB: Number.isFinite(sz) ? Math.round(sz) : null,
      latencyMs: Number.isFinite(lat) ? Math.round(lat) : null,
      isDefault: !!v.isDefault,
      status,
      statusClass,
    };
  });
});

/**
 * V1 `installPaths` parity (ModelInfoDrawer.js:100-119). Multi-variant Pack →
 * one labeled row per variant that has an installPath; otherwise the
 * single top-level installPath as one unlabeled row.
 */
const installPaths = computed(() => {
  const arr = (model.value?.variants ?? []) as DrawerVariantVM[];
  if (arr.length >= 2) {
    const rows = arr
      .filter((v) => !!v.installPath)
      .map((v) => ({
        id: v.id || "",
        label: v.label || v.id || "",
        isDefault: !!v.isDefault,
        path: v.installPath as string,
      }));
    if (rows.length > 0) return rows;
  }
  const single = model.value?.installPath;
  return single ? [{ id: "", label: "", isDefault: false, path: single }] : [];
});

function onSelectVariant(v: { id: string; status: string }): void {
  if (!v.id) return;
  if (v.status !== "Ready") return;
  emit("select-variant", v.id);
}

function onApplyExample(ex: { name?: string | null; license?: string | null; inputs?: Record<string, unknown>; paramsOverride?: Record<string, unknown> }): void {
  emit("apply-example", ex);
}

// ── Delete panel logic (V1 ModelInfoDrawer.js:135-303) ───────────────────────
const { confirm } = useConfirm();
const deleting = ref(false);
const deletePanelOpen = ref(false);
const deleteCheckedIds = ref<string[]>([]);

/** Whether the model is user-imported (non-registry) and thus deletable. */
const isUserImported = computed<boolean>(() => !!(model.value as DrawerModelVM | null)?.userImported);

function allVariantIds(): string[] {
  return variantsList.value.map((v) => v.id).filter(Boolean);
}

const willDeleteAll = computed<boolean>(() => {
  const all = allVariantIds();
  return all.length > 0 && deleteCheckedIds.value.length === all.length;
});

const canConfirmDelete = computed<boolean>(() => deleteCheckedIds.value.length > 0);

function toggleDeleteCheck(vid: string): void {
  if (deleting.value) return;
  const idx = deleteCheckedIds.value.indexOf(vid);
  if (idx >= 0) {
    deleteCheckedIds.value = deleteCheckedIds.value.filter((x) => x !== vid);
  } else {
    deleteCheckedIds.value = [...deleteCheckedIds.value, vid];
  }
}

function selectAllDelete(): void {
  if (deleting.value) return;
  deleteCheckedIds.value = [...allVariantIds()];
}

function deselectAllDelete(): void {
  if (deleting.value) return;
  deleteCheckedIds.value = [];
}

function closeDeletePanel(): void {
  if (deleting.value) return;
  deletePanelOpen.value = false;
  deleteCheckedIds.value = [];
}

/**
 * V1 parity: single-variant → immediate confirm dialog → emit 'delete-model'.
 * Multi-variant → open inline picker panel.
 */
function openDeletePanel(): void {
  const ids = allVariantIds();
  if (ids.length <= 1) {
    void confirmFullDelete();
    return;
  }
  deleteCheckedIds.value = [...ids]; // Default: all checked
  deletePanelOpen.value = true;
}

/** Full model delete (single variant or user chose "delete all" in picker). */
async function confirmFullDelete(): Promise<void> {
  const name = model.value?.displayName || model.value?.modelId || "";
  const ok = await confirm({
    icon: "\u{1F5D1}\uFE0F",
    title: t("appBuilder.deleteModel"),
    message: t("appBuilder.confirmDeleteModel", { name }),
    confirmText: t("common.delete"),
    cancelText: t("common.cancel"),
    confirmStyle: "danger",
  });
  if (!ok) return;
  deleting.value = true;
  try {
    emit("delete-model", model.value!.modelId);
  } finally {
    deleting.value = false;
  }
}

/**
 * Perform delete from the picker panel (partial or all variants).
 * V1 ModelInfoDrawer.js:227-303.
 */
async function performDelete(): Promise<void> {
  const checkedIds = [...deleteCheckedIds.value];
  if (checkedIds.length === 0) return;

  const name = model.value?.displayName || model.value?.modelId || "";
  const isAll = willDeleteAll.value;

  const ok = await confirm({
    icon: "\u{1F5D1}\uFE0F",
    title: isAll ? t("appBuilder.deleteModel") : t("appBuilder.deleteVariants.title"),
    message: isAll
      ? t("appBuilder.confirmDeleteModel", { name })
      : t("appBuilder.deleteVariants.confirmPartial", { name, n: checkedIds.length, ids: checkedIds.join(", ") }),
    confirmText: t("common.delete"),
    cancelText: t("common.cancel"),
    confirmStyle: "danger",
  });
  if (!ok) return;

  deleting.value = true;
  try {
    if (isAll) {
      emit("delete-model", model.value!.modelId);
    } else {
      emit("delete-variants", model.value!.modelId, checkedIds);
      // Reset picker after partial delete
      deletePanelOpen.value = false;
      deleteCheckedIds.value = [];
    }
  } finally {
    deleting.value = false;
  }
}
</script>

<template>
  <Teleport to="body">
    <template v-if="open && model">
      <div
        class="ab-drawer-backdrop"
        @click="emit('close')"
      ></div>
      <aside
        class="ab-info-drawer"
        role="dialog"
        aria-modal="true"
      >
        <header class="ab-drawer-header">
          <div class="ab-drawer-title">
            <span class="ab-drawer-name">{{ model.displayName || model.modelId }}</span>
            <span
              v-if="model.version"
              class="ab-drawer-version"
            >v{{ model.version }}</span>
          </div>
          <button
            type="button"
            class="ab-drawer-close"
            :title="t('appBuilder.close')"
            @click="emit('close')"
          >
            ×
          </button>
        </header>

        <div class="ab-drawer-body">
          <p
            v-if="model.description"
            class="ab-drawer-md"
          >
            {{ model.description }}
          </p>

          <!-- V1 ModelInfoDrawer.js:333-337: longDescription is the second
               paragraph of the drawer body, NOT a footer section. Pulling it
               here keeps "what does this model do" close to the title. -->
          <p
            v-if="model.longDescription"
            class="ab-drawer-md ab-drawer-md-long"
          >
            {{ model.longDescription }}
          </p>

          <div class="ab-drawer-section">
            <div class="ab-drawer-label">
              {{ t("appBuilder.category") }}
            </div>
            <div class="ab-drawer-value">
              {{ model.category || "—" }}
            </div>
          </div>

          <div
            v-if="runtimeText"
            class="ab-drawer-section"
          >
            <div class="ab-drawer-label">
              {{ t("appBuilder.metrics.quantization") }}
            </div>
            <div class="ab-drawer-value ab-drawer-mono">
              {{ runtimeText }}
            </div>
          </div>

          <div
            v-if="latencyText || memoryText"
            class="ab-drawer-section"
          >
            <div class="ab-drawer-label">
              {{ t("appBuilder.metrics.title") }}
            </div>
            <div class="ab-drawer-value ab-drawer-mono">
              <span v-if="latencyText">{{ latencyText }}</span>
              <span v-if="latencyText && memoryText"> · </span>
              <span v-if="memoryText">{{ memoryText }}</span>
            </div>
          </div>

          <div
            v-if="model.vendor"
            class="ab-drawer-section"
          >
            <div class="ab-drawer-label">
              {{ t("appBuilder.vendor") }}
            </div>
            <div class="ab-drawer-value">
              {{ model.vendor }}
            </div>
          </div>

          <!-- ── Variants (V1 ModelInfoDrawer.js:350-374) ─────────────── -->
          <div
            v-if="variantsList.length >= 1"
            class="ab-drawer-section ab-drawer-variants"
          >
            <div class="ab-drawer-label">
              {{ t("appBuilder.variant.title") }}
              <span class="ab-drawer-variants-count">({{ variantsList.length }})</span>
            </div>
            <ul class="ab-drawer-variants-list">
              <li
                v-for="v in variantsList"
                :key="v.id"
                class="ab-drawer-variant-item"
                :class="{
                  'is-active': v.id === props.selectedVariantId,
                  'is-disabled': v.status !== 'Ready',
                }"
                :title="v.longLabel || v.label"
                @click="onSelectVariant(v)"
              >
                <span
                  class="ab-status-dot"
                  :class="v.statusClass"
                  aria-hidden="true"
                ></span>
                <span class="ab-drawer-variant-label">{{ v.label }}</span>
                <span
                  v-if="v.isDefault"
                  class="ab-drawer-variant-default"
                >{{ t("appBuilder.variant.default") }}</span>
                <span
                  v-if="v.quantization"
                  class="ab-drawer-variant-quant"
                >{{ String(v.quantization).toUpperCase() }}</span>
                <span
                  v-if="v.sizeMB != null"
                  class="ab-drawer-variant-size"
                >{{ t("appBuilder.variant.sizeOnDisk", { n: v.sizeMB }) }}</span>
                <span
                  v-if="v.latencyMs != null"
                  class="ab-drawer-variant-latency"
                >{{ t("appBuilder.variant.latencyHint", { n: v.latencyMs }) }}</span>
                <span
                  v-if="v.status !== 'Ready'"
                  class="ab-drawer-variant-missing"
                >{{ t("appBuilder.variant.missing") }}</span>
              </li>
            </ul>
          </div>

          <!-- ── Tags (V1 ModelInfoDrawer.js:376-381) ─────────────────── -->
          <div
            v-if="tags.length"
            class="ab-drawer-section"
          >
            <div class="ab-drawer-label">
              {{ t("appBuilder.tags") }}
            </div>
            <div class="ab-drawer-tags">
              <span
                v-for="tag in tags"
                :key="tag"
                class="ab-drawer-tag"
              >{{ tag }}</span>
            </div>
          </div>

          <!-- ── Capabilities (V1 ModelInfoDrawer.js:383-388) ─────────── -->
          <div
            v-if="capabilities.length"
            class="ab-drawer-section"
          >
            <div class="ab-drawer-label">
              {{ t("appBuilder.capabilities") }}
            </div>
            <div class="ab-drawer-tags">
              <span
                v-for="c in capabilities"
                :key="c"
                class="ab-drawer-tag"
              >{{ c }}</span>
            </div>
          </div>

          <!-- ── Examples (V1 ModelInfoDrawer.js:390-402) ─────────────── -->
          <div
            v-if="examples.length"
            class="ab-drawer-section"
          >
            <div class="ab-drawer-label">
              {{ t("appBuilder.examples") }}
            </div>
            <ul class="ab-drawer-examples">
              <li
                v-for="(ex, i) in examples"
                :key="i"
                class="ab-drawer-example"
              >
                <button
                  type="button"
                  class="ab-drawer-example-btn"
                  :title="t('appBuilder.applyExample')"
                  @click="onApplyExample(ex)"
                >
                  <span class="ab-drawer-example-name">{{ ex.name || `Example #${i + 1}` }}</span>
                  <span
                    v-if="ex.license"
                    class="ab-drawer-example-license"
                  >{{ ex.license }}</span>
                </button>
              </li>
            </ul>
          </div>

          <!-- ── Weights URL (V1 ModelInfoDrawer.js:404-407) ──────────── -->
          <div
            v-if="weightsUrl"
            class="ab-drawer-section"
          >
            <div class="ab-drawer-label">
              {{ t("appBuilder.weights") }}
            </div>
            <div class="ab-drawer-value ab-drawer-mono">
              {{ weightsUrl }}
            </div>
          </div>

          <!-- ── Install Path (V1 ModelInfoDrawer.js:409-418) ─────────── -->
          <div
            v-if="installPaths.length"
            class="ab-drawer-section"
          >
            <div class="ab-drawer-label">
              {{ t("appBuilder.installPath") }}
            </div>
            <div
              v-for="row in installPaths"
              :key="row.id || 'single'"
              class="ab-drawer-install-row"
            >
              <span
                v-if="row.label"
                class="ab-drawer-install-label"
              >
                {{ row.label }}<span
                  v-if="row.isDefault"
                  class="ab-drawer-install-default"
                >{{ t("appBuilder.variant.default") }}</span>
              </span>
              <div class="ab-drawer-value ab-drawer-mono ab-drawer-install-path">
                {{ row.path }}
              </div>
            </div>
          </div>

          <!-- ── Delete panel (V1 ModelInfoDrawer.js:420-472) ──────────── -->
          <div
            v-if="isUserImported"
            class="model-info-delete"
          >
            <!-- Multi-variant inline picker (V1 ModelInfoDrawer.js:426-466) -->
            <div
              v-if="deletePanelOpen"
              class="ab-drawer-delete-panel"
            >
              <div class="ab-drawer-delete-title">
                {{ t("appBuilder.deleteVariants.pickerTitle") }}
              </div>
              <div
                class="ab-drawer-delete-actions"
                style="margin-bottom: 6px;"
              >
                <button
                  type="button"
                  class="ab-drawer-delete-cancel"
                  :disabled="deleting"
                  @click="selectAllDelete"
                >
                  {{ t("appBuilder.deleteVariants.selectAll") }}
                </button>
                <button
                  type="button"
                  class="ab-drawer-delete-cancel"
                  :disabled="deleting"
                  @click="deselectAllDelete"
                >
                  {{ t("appBuilder.deleteVariants.deselectAll") }}
                </button>
              </div>
              <ul class="ab-drawer-delete-list">
                <li
                  v-for="v in variantsList"
                  :key="'del-' + v.id"
                  class="ab-drawer-delete-item"
                >
                  <label class="ab-drawer-delete-row">
                    <input
                      type="checkbox"
                      :checked="deleteCheckedIds.includes(v.id)"
                      :disabled="deleting"
                      @change="toggleDeleteCheck(v.id)"
                    />
                    <span class="ab-drawer-delete-label">{{ v.label }}</span>
                    <span
                      v-if="v.isDefault"
                      class="ab-drawer-delete-default"
                    >{{ t("appBuilder.variant.default") }}</span>
                    <span
                      v-if="v.quantization"
                      class="ab-drawer-delete-quant"
                    >{{ String(v.quantization).toUpperCase() }}</span>
                    <span
                      v-if="v.sizeMB != null"
                      class="ab-drawer-delete-size"
                    >{{ t("appBuilder.variant.sizeOnDisk", { n: v.sizeMB }) }}</span>
                  </label>
                </li>
              </ul>
              <p
                v-if="willDeleteAll"
                class="ab-drawer-delete-warn"
              >
                ⚠ {{ t("appBuilder.deleteVariants.warnAll") }}
              </p>
              <p
                v-else
                class="ab-drawer-delete-hint"
              >
                {{ t("appBuilder.deleteVariants.hintPartial") }}
              </p>
              <div class="ab-drawer-delete-actions">
                <button
                  type="button"
                  class="ab-drawer-delete-cancel"
                  :disabled="deleting"
                  @click="closeDeletePanel"
                >
                  {{ t("common.cancel") }}
                </button>
                <button
                  type="button"
                  class="ab-drawer-delete-go"
                  :disabled="!canConfirmDelete || deleting"
                  @click="performDelete"
                >
                  {{ deleting
                    ? t("common.loading")
                    : (willDeleteAll
                      ? t("appBuilder.deleteVariants.deleteAll")
                      : t("appBuilder.deleteVariants.deleteSelected", { n: deleteCheckedIds.length })) }}
                </button>
              </div>
            </div>
            <!-- Trigger button (hidden while picker is open) — V1 ModelInfoDrawer.js:468-471 -->
            <button
              v-else
              class="model-info-delete-btn"
              :disabled="deleting"
              @click="openDeletePanel"
            >
              {{ deleting ? t("common.loading") : t("appBuilder.deleteModel") }}
            </button>
          </div>
        </div>
      </aside>
    </template>
  </Teleport>
</template>
