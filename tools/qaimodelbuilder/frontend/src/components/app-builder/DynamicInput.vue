<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * DynamicInput — schema-driven multi-field input form.
 *
 * Renders one form control per schema field (text / textarea / number /
 * range / select / boolean / radio / file / json). Used by AppBuilder
 * workbench when a model registers a multi-field `input_schema` (single-kind
 * image/audio/text models go through dedicated `inputs/*` components).
 *
 * Design notes
 * ------------
 * - The exported `SchemaField` in `composables/app-builder/useAppBuilderWorkbench.ts`
 *   is intentionally narrow (key/type/label/options/placeholder). This component
 *   accepts a structurally compatible **superset** (`DynamicInputField`) so a
 *   richer manifest schema (rows / min / max / step / accept / required /
 *   helpText / object-form options) can flow through without forcing the
 *   composable to know about every UI-only knob. Narrow `SchemaField[]` is a
 *   subtype of `DynamicInputField[]` and assigns through covariantly.
 * - All control types use the global `ab-param-*` classes (no scoped CSS
 *   here) so spacing / focus / toggle styling stay aligned with `DynamicParams`
 *   without duplication — single source of truth for AppBuilder form chrome.
 * - Required (`*`) marker, helpText line and placeholder/help meta are
 *   rendered uniformly per row, regardless of control kind.
 */

import { computed } from "vue";
import { useI18n } from "vue-i18n";

const { t } = useI18n();

/** One option entry for select / radio (string OR {label, value}). */
interface OptionObject {
  label: string;
  value: string | number | boolean;
}

/**
 * Field descriptor accepted by this component.
 *
 * Superset of the narrow `SchemaField` exported by the workbench composable;
 * extra fields are all optional, so a `SchemaField[]` from the composable
 * assigns into `DynamicInputField[]` without needing changes upstream.
 */
interface DynamicInputField {
  key: string;
  label: string;
  type:
    | "text"
    | "textarea"
    | "number"
    | "integer"
    | "range"
    | "select"
    | "boolean"
    | "checkbox"
    | "toggle"
    | "radio"
    | "file"
    | "json";
  /** Visual `*` marker + (best-effort) emit gating. */
  required?: boolean;
  /** Inline help text shown below the control. */
  helpText?: string;
  /** Placeholder for text-like inputs. */
  placeholder?: string;
  /** textarea / json rows. */
  rows?: number;
  /** number / range bounds & step. */
  min?: number;
  max?: number;
  step?: number;
  /** text / textarea max length. */
  maxLength?: number;
  /** select / radio options (string array or {label, value} array). */
  options?: Array<string | OptionObject>;
  /** file input accept attribute (e.g. "image/*", ".csv,.tsv"). */
  accept?: string;
}

interface Props {
  schema: DynamicInputField[];
  modelValue: Record<string, unknown>;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  "update:modelValue": [value: Record<string, unknown>];
}>();

/** Whether at least one required field is currently empty. */
const hasMissingRequired = computed(() => {
  for (const f of props.schema) {
    if (f.required !== true) continue;
    const v = props.modelValue[f.key];
    if (v === undefined || v === null) return true;
    if (typeof v === "string" && v.trim() === "") return true;
  }
  return false;
});

defineExpose({ hasMissingRequired });

// ── helpers ──────────────────────────────────────────────────────────────

function update(key: string, value: unknown): void {
  emit("update:modelValue", { ...props.modelValue, [key]: value });
}

function valueOf(field: DynamicInputField): unknown {
  return props.modelValue[field.key];
}

function stringValue(field: DynamicInputField): string {
  const v = valueOf(field);
  return typeof v === "string" ? v : v === undefined || v === null ? "" : String(v);
}

function numberValue(field: DynamicInputField): number | "" {
  const v = valueOf(field);
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return "";
}

function rangeValue(field: DynamicInputField): number {
  const v = numberValue(field);
  if (v !== "") return v;
  // Fall back to min so the slider thumb has a defined position.
  if (typeof field.min === "number") return field.min;
  return 0;
}

function booleanValue(field: DynamicInputField): boolean {
  return valueOf(field) === true;
}

/** Normalise options to a uniform `{label, value}[]` shape for v-for. */
function optionList(field: DynamicInputField): OptionObject[] {
  const opts = field.options;
  if (!Array.isArray(opts)) return [];
  return opts.map((o) =>
    typeof o === "string" || typeof o === "number" || typeof o === "boolean"
      ? { label: String(o), value: o }
      : { label: o.label, value: o.value },
  );
}

/** stringified value for radio comparison (radios always carry string `value`). */
function radioStringValue(field: DynamicInputField): string {
  const v = valueOf(field);
  return v === undefined || v === null ? "" : String(v);
}

function selectStringValue(field: DynamicInputField): string {
  const v = valueOf(field);
  return v === undefined || v === null ? "" : String(v);
}

/** Map a stringified <option value="..."> back to the original typed value. */
function pickOptionValue(field: DynamicInputField, raw: string): unknown {
  for (const o of optionList(field)) {
    if (String(o.value) === raw) return o.value;
  }
  return raw;
}

function onNumberInput(field: DynamicInputField, raw: string): void {
  if (raw === "") {
    update(field.key, "");
    return;
  }
  const n = Number(raw);
  if (Number.isFinite(n)) {
    update(field.key, field.type === "integer" ? Math.trunc(n) : n);
  }
}

function onSelectChange(field: DynamicInputField, raw: string): void {
  update(field.key, pickOptionValue(field, raw));
}

function onRadioChange(field: DynamicInputField, optValue: string | number | boolean): void {
  update(field.key, optValue);
}

/**
 * File input → base64 dataURL.
 *
 * NOTE: For larger payloads the proper path is the `/api/uploads` endpoint
 * (returns a stable handle); this component currently inlines the file as a
 * `data:` URL for simplicity. Swap to the upload service once the multi-field
 * schema models that actually need this land — keep behaviour and emit shape
 * (string `dataURL` or upload handle) compatible at that point.
 */
function onFileChange(field: DynamicInputField, evt: Event): void {
  const target = evt.target as HTMLInputElement;
  const file = target.files !== null && target.files.length > 0 ? target.files.item(0) : null;
  if (file === null) {
    update(field.key, null);
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    const result = reader.result;
    update(field.key, typeof result === "string" ? result : null);
  };
  reader.readAsDataURL(file);
}

/** JSON textarea: store the *parsed* value when valid, else keep raw string. */
function onJsonInput(field: DynamicInputField, raw: string): void {
  if (raw === "") {
    update(field.key, "");
    return;
  }
  try {
    const parsed = JSON.parse(raw) as unknown;
    update(field.key, parsed);
  } catch {
    // Keep the raw text so the user can keep typing; validity is reflected
    // via `jsonError(field)` below for inline feedback.
    update(field.key, raw);
  }
}

function jsonError(field: DynamicInputField): string | null {
  const v = valueOf(field);
  if (typeof v !== "string" || v.trim() === "") return null;
  try {
    JSON.parse(v);
    return null;
  } catch (e) {
    return e instanceof Error ? e.message : t("appBuilder.invalidJson");
  }
}

/** Pretty-print parsed JSON values back to text for the textarea. */
function jsonDisplay(field: DynamicInputField): string {
  const v = valueOf(field);
  if (v === undefined || v === null) return "";
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function isCheckboxLike(t: DynamicInputField["type"]): boolean {
  return t === "boolean" || t === "checkbox" || t === "toggle";
}

function isNumberLike(t: DynamicInputField["type"]): boolean {
  return t === "number" || t === "integer";
}
</script>

<template>
  <div class="ab-input-form">
    <div
      v-for="field in schema"
      :key="field.key"
      class="ab-param-row ab-input-row"
      :class="{ 'ab-input-row--block': field.type === 'textarea' || field.type === 'json' || field.type === 'radio' }"
    >
      <label
        class="ab-param-label"
        :for="'ab-in-' + field.key"
      >
        <span>{{ field.label }}</span>
        <span
          v-if="field.required === true"
          class="ab-input-required"
          aria-hidden="true"
        >*</span>
      </label>

      <div class="ab-input-control">
        <!-- text -->
        <input
          v-if="field.type === 'text'"
          :id="'ab-in-' + field.key"
          type="text"
          class="ab-param-select"
          :value="stringValue(field)"
          :placeholder="field.placeholder"
          :maxlength="field.maxLength"
          :required="field.required === true"
          @input="update(field.key, ($event.target as HTMLInputElement).value)"
        />

        <!-- textarea -->
        <textarea
          v-else-if="field.type === 'textarea'"
          :id="'ab-in-' + field.key"
          class="ab-param-textarea"
          :rows="field.rows ?? 4"
          :placeholder="field.placeholder"
          :maxlength="field.maxLength"
          :required="field.required === true"
          :value="stringValue(field)"
          @input="update(field.key, ($event.target as HTMLTextAreaElement).value)"
        ></textarea>

        <!-- number / integer -->
        <input
          v-else-if="isNumberLike(field.type)"
          :id="'ab-in-' + field.key"
          type="number"
          class="ab-param-numinput"
          :min="field.min"
          :max="field.max"
          :step="field.step ?? (field.type === 'integer' ? 1 : undefined)"
          :placeholder="field.placeholder"
          :required="field.required === true"
          :value="numberValue(field)"
          @input="onNumberInput(field, ($event.target as HTMLInputElement).value)"
        />

        <!-- range: slider + paired numinput + meta -->
        <div
          v-else-if="field.type === 'range'"
          class="ab-param-number"
        >
          <input
            type="range"
            class="ab-param-range"
            :min="field.min"
            :max="field.max"
            :step="field.step"
            :value="rangeValue(field)"
            @input="onNumberInput(field, ($event.target as HTMLInputElement).value)"
          />
          <input
            :id="'ab-in-' + field.key"
            type="number"
            class="ab-param-numinput"
            :min="field.min"
            :max="field.max"
            :step="field.step"
            :value="numberValue(field)"
            @input="onNumberInput(field, ($event.target as HTMLInputElement).value)"
          />
          <span
            v-if="field.min !== undefined && field.max !== undefined"
            class="ab-param-range-meta"
          >
            <span>{{ field.min }}</span>
            <span class="ab-param-range-meta-sep">–</span>
            <span>{{ field.max }}</span>
          </span>
        </div>

        <!-- select -->
        <select
          v-else-if="field.type === 'select'"
          :id="'ab-in-' + field.key"
          class="ab-param-select"
          :value="selectStringValue(field)"
          :required="field.required === true"
          @change="onSelectChange(field, ($event.target as HTMLSelectElement).value)"
        >
          <option
            v-if="field.placeholder !== undefined && field.placeholder !== ''"
            value=""
            disabled
          >
            {{ field.placeholder }}
          </option>
          <option
            v-for="opt in optionList(field)"
            :key="String(opt.value)"
            :value="String(opt.value)"
          >
            {{ opt.label }}
          </option>
        </select>

        <!-- boolean / checkbox / toggle -->
        <label
          v-else-if="isCheckboxLike(field.type)"
          class="toggle ab-param-toggle"
        >
          <input
            :id="'ab-in-' + field.key"
            type="checkbox"
            :checked="booleanValue(field)"
            @change="update(field.key, ($event.target as HTMLInputElement).checked)"
          />
          <span class="toggle-slider"></span>
        </label>

        <!-- radio group -->
        <div
          v-else-if="field.type === 'radio'"
          class="ab-input-radio-group"
          role="radiogroup"
          :aria-labelledby="'ab-in-' + field.key"
        >
          <label
            v-for="opt in optionList(field)"
            :key="String(opt.value)"
            class="ab-input-radio-item"
          >
            <input
              type="radio"
              :name="'ab-in-' + field.key"
              :value="String(opt.value)"
              :checked="radioStringValue(field) === String(opt.value)"
              @change="onRadioChange(field, opt.value)"
            />
            <span>{{ opt.label }}</span>
          </label>
        </div>

        <!-- file -->
        <div
          v-else-if="field.type === 'file'"
          class="ab-input-file"
        >
          <input
            :id="'ab-in-' + field.key"
            type="file"
            class="ab-input-file-input"
            :accept="field.accept"
            :required="field.required === true"
            @change="onFileChange(field, $event)"
          />
        </div>

        <!-- json -->
        <template v-else-if="field.type === 'json'">
          <textarea
            :id="'ab-in-' + field.key"
            class="ab-param-textarea ab-input-json"
            :rows="field.rows ?? 8"
            :placeholder="field.placeholder ?? '{ }'"
            :required="field.required === true"
            spellcheck="false"
            :value="jsonDisplay(field)"
            @input="onJsonInput(field, ($event.target as HTMLTextAreaElement).value)"
          ></textarea>
          <div
            v-if="jsonError(field) !== null"
            class="ab-input-json-error"
            role="alert"
          >
            {{ jsonError(field) }}
          </div>
        </template>

        <!-- unknown / fallback -->
        <input
          v-else
          :id="'ab-in-' + field.key"
          type="text"
          class="ab-param-select"
          :value="stringValue(field)"
          :placeholder="field.placeholder"
          @input="update(field.key, ($event.target as HTMLInputElement).value)"
        />

        <p
          v-if="field.helpText !== undefined && field.helpText !== ''"
          class="ab-input-help"
        >
          {{ field.helpText }}
        </p>
      </div>
    </div>
  </div>
</template>

<style scoped>
/*
 * Most form chrome (label / select / numinput / textarea / range / toggle)
 * is inherited from the global `ab-param-*` classes defined in
 * styles/app-builder/app-builder.css §10. Only the small additions specific
 * to multi-field input rows live here.
 */

.ab-input-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* For block-style controls (textarea / json / radio group) the label sits on
 * its own row so the control gets full width. */
.ab-input-row--block {
  grid-template-columns: 1fr;
  align-items: start;
  gap: var(--space-1);
}

.ab-input-control {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

.ab-input-required {
  margin-left: 4px;
  color: var(--danger, var(--text-error, #e5484d));
  font-weight: var(--weight-bold);
}

.ab-input-help {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--text-xs);
  line-height: 1.4;
}

.ab-input-radio-group {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2) var(--space-3);
}

.ab-input-radio-item {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--text-sm);
  color: var(--text-primary);
  cursor: pointer;
}

.ab-input-file-input {
  font-size: var(--text-sm);
  color: var(--text-primary);
}

.ab-input-json {
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  min-height: 120px;
}

.ab-input-json-error {
  color: var(--danger, var(--text-error, #e5484d));
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  white-space: pre-wrap;
}
</style>
