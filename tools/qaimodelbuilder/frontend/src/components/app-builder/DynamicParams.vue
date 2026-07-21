<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * DynamicParams — schema-driven inference parameter panel.
 *
 * V1 parity (`DynamicParams.js`): splits params into a permanently-expanded
 * basic group and a collapsible advanced group, and renders each by type:
 *   - select  → <select> (label/value options)
 *   - number  → range slider + number input + min–max meta (or plain number
 *     input when the range is large, >100)
 *   - boolean → toggle switch (global `.toggle` / `.toggle-slider`)
 *   - text    → <textarea rows=2>
 *
 * Labels resolve through `appBuilder.params.<name>` i18n keys (falling back to
 * the manifest label), so e.g. `vad` shows the short "VAD".
 *
 * Implementation note (architecture): the control-type dispatch + basic/advanced
 * split live here as a cohesive, typed component (vs V1's untyped template),
 * reusing the global `ab-param-*` design tokens — not a copy of V1's JS shape.
 */
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

interface ParamOption {
  label: string;
  value: unknown;
}

interface ParamDef {
  key: string;
  name: string;
  label: string;
  type: "number" | "string" | "boolean" | "select" | "text";
  default?: unknown;
  min?: number;
  max?: number;
  step?: number;
  options?: ParamOption[];
  advanced?: boolean;
}

interface Props {
  params: ParamDef[];
  modelValue: Record<string, unknown>;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  "update:modelValue": [value: Record<string, unknown>];
}>();

const { t } = useI18n();

const advancedOpen = ref(false);

const values = computed(() => props.modelValue);

const basicParams = computed(() => props.params.filter((p) => p.advanced !== true));
const advancedParams = computed(() => props.params.filter((p) => p.advanced === true));

/**
 * V1 parity (`DynamicParams.js:46-114`): when the param schema changes (usually
 * a model switch) auto-fill any missing key with its `default` (or `false` for
 * boolean / `""` for text) and emit, so the backend never receives a dict that
 * is missing parameter keys. Computes the "should-be" value map and only emits
 * when it actually differs from the current `modelValue` (shallow), avoiding
 * feedback loops.
 */
function computeFilled(
  params: ParamDef[],
  current: Record<string, unknown>,
): Record<string, unknown> | null {
  if (params.length === 0) return null;
  const next = { ...current };
  let changed = false;
  for (const p of params) {
    if (p.key in next) continue;
    if ("default" in p && p.default !== undefined) {
      next[p.key] = p.default;
      changed = true;
    } else if (p.type === "boolean") {
      next[p.key] = false;
      changed = true;
    } else if (p.type === "text") {
      next[p.key] = "";
      changed = true;
    }
  }
  return changed ? next : null;
}

watch(
  () => props.params,
  (params) => {
    const filled = computeFilled(params, props.modelValue);
    if (filled !== null) emit("update:modelValue", filled);
  },
  { immediate: true },
);

// V1 parity: resolve label via `appBuilder.params.<name>` i18n key, fall back
// to the manifest label.
function paramLabel(param: ParamDef): string {
  return t("appBuilder.params." + param.name, param.label || param.name);
}

function valueOf(param: ParamDef): unknown {
  const cur = values.value;
  return param.key in cur ? cur[param.key] : param.default;
}

function update(key: string, value: unknown): void {
  if (values.value[key] === value) return;
  emit("update:modelValue", { ...values.value, [key]: value });
}

// number: use a range slider unless the span is large (>100) — then plain input.
function isLargeRange(param: ParamDef): boolean {
  if (typeof param.min !== "number" || typeof param.max !== "number") return true;
  return param.max - param.min > 100;
}
function stepOf(param: ParamDef): number {
  return typeof param.step === "number" && param.step > 0 ? param.step : 1;
}
function onNumber(param: ParamDef, raw: string): void {
  if (raw === "") {
    update(param.key, typeof param.default === "number" ? param.default : 0);
    return;
  }
  const n = Number(raw);
  if (!Number.isFinite(n)) return;
  const min = typeof param.min === "number" ? param.min : -Infinity;
  const max = typeof param.max === "number" ? param.max : Infinity;
  update(param.key, Math.max(min, Math.min(max, n)));
}

// select: bind by option index so non-string values round-trip cleanly.
function selectIndex(param: ParamDef): string {
  const opts = param.options ?? [];
  const cur = valueOf(param);
  for (let i = 0; i < opts.length; i++) {
    if (opts[i]?.value === cur) return String(i);
  }
  return "";
}
function onSelect(param: ParamDef, idxRaw: string): void {
  const idx = Number(idxRaw);
  const opt = (param.options ?? [])[idx];
  if (opt !== undefined) update(param.key, opt.value);
}

function placeholderFor(param: ParamDef): string {
  return param.default != null ? String(param.default) : "";
}
</script>

<template>
  <div class="ab-params">
    <div
      v-if="params.length === 0"
      class="ab-params-empty"
    >
      {{ t("appBuilder.noParams") }}
    </div>

    <template v-else>
      <!-- basic group (always expanded) -->
      <div
        v-if="basicParams.length > 0"
        class="ab-params-group ab-params-basic"
      >
        <div
          v-for="param in basicParams"
          :key="param.key"
          class="ab-param-row"
        >
          <label
            class="ab-param-label"
            :for="'ab-p-' + param.key"
          >{{ paramLabel(param) }}</label>

          <!-- select -->
          <select
            v-if="param.type === 'select'"
            :id="'ab-p-' + param.key"
            class="ab-param-select"
            :value="selectIndex(param)"
            @change="onSelect(param, ($event.target as HTMLSelectElement).value)"
          >
            <option
              v-for="(opt, idx) in param.options ?? []"
              :key="idx"
              :value="idx"
            >
              {{ opt.label }}
            </option>
          </select>

          <!-- number: range + number + meta -->
          <div
            v-else-if="param.type === 'number'"
            class="ab-param-number"
          >
            <input
              v-if="!isLargeRange(param)"
              type="range"
              class="ab-param-range"
              :min="param.min"
              :max="param.max"
              :step="stepOf(param)"
              :value="valueOf(param) as number"
              @input="onNumber(param, ($event.target as HTMLInputElement).value)"
            />
            <input
              :id="'ab-p-' + param.key"
              type="number"
              class="ab-param-numinput"
              :min="param.min"
              :max="param.max"
              :step="stepOf(param)"
              :value="valueOf(param) as number"
              :placeholder="placeholderFor(param)"
              @input="onNumber(param, ($event.target as HTMLInputElement).value)"
            />
            <span
              v-if="param.min != null && param.max != null"
              class="ab-param-range-meta"
            >
              <span>{{ param.min }}</span>
              <span class="ab-param-range-meta-sep">–</span>
              <span>{{ param.max }}</span>
            </span>
          </div>

          <!-- boolean: toggle switch -->
          <label
            v-else-if="param.type === 'boolean'"
            class="toggle ab-param-toggle"
          >
            <input
              :id="'ab-p-' + param.key"
              type="checkbox"
              :checked="!!valueOf(param)"
              @change="update(param.key, ($event.target as HTMLInputElement).checked)"
            />
            <span class="toggle-slider"></span>
          </label>

          <!-- text: textarea -->
          <textarea
            v-else-if="param.type === 'text'"
            :id="'ab-p-' + param.key"
            class="ab-param-textarea"
            rows="2"
            :placeholder="placeholderFor(param)"
            :value="(valueOf(param) as string) ?? ''"
            @input="update(param.key, ($event.target as HTMLTextAreaElement).value)"
          ></textarea>

          <!-- string fallback -->
          <input
            v-else
            type="text"
            class="ab-param-select"
            :value="(valueOf(param) as string) ?? ''"
            @input="update(param.key, ($event.target as HTMLInputElement).value)"
          />
        </div>
      </div>

      <!-- advanced group (collapsible) -->
      <div
        v-if="advancedParams.length > 0"
        class="ab-params-group ab-params-advanced"
      >
        <button
          type="button"
          class="ab-params-advanced-toggle"
          :aria-expanded="advancedOpen ? 'true' : 'false'"
          @click="advancedOpen = !advancedOpen"
        >
          <span
            class="ab-params-advanced-arrow"
            aria-hidden="true"
          >{{ advancedOpen ? "▾" : "▸" }}</span>
          <span>{{ t("appBuilder.advancedParams") }}</span>
        </button>

        <div
          v-show="advancedOpen"
          class="ab-params-advanced-body"
        >
          <div
            v-for="param in advancedParams"
            :key="param.key"
            class="ab-param-row"
          >
            <label
              class="ab-param-label"
              :for="'ab-pa-' + param.key"
            >{{ paramLabel(param) }}</label>

            <select
              v-if="param.type === 'select'"
              :id="'ab-pa-' + param.key"
              class="ab-param-select"
              :value="selectIndex(param)"
              @change="onSelect(param, ($event.target as HTMLSelectElement).value)"
            >
              <option
                v-for="(opt, idx) in param.options ?? []"
                :key="idx"
                :value="idx"
              >
                {{ opt.label }}
              </option>
            </select>

            <div
              v-else-if="param.type === 'number'"
              class="ab-param-number"
            >
              <input
                v-if="!isLargeRange(param)"
                type="range"
                class="ab-param-range"
                :min="param.min"
                :max="param.max"
                :step="stepOf(param)"
                :value="valueOf(param) as number"
                @input="onNumber(param, ($event.target as HTMLInputElement).value)"
              />
              <input
                :id="'ab-pa-' + param.key"
                type="number"
                class="ab-param-numinput"
                :min="param.min"
                :max="param.max"
                :step="stepOf(param)"
                :value="valueOf(param) as number"
                :placeholder="placeholderFor(param)"
                @input="onNumber(param, ($event.target as HTMLInputElement).value)"
              />
              <span
                v-if="param.min != null && param.max != null"
                class="ab-param-range-meta"
              >
                <span>{{ param.min }}</span>
                <span class="ab-param-range-meta-sep">–</span>
                <span>{{ param.max }}</span>
              </span>
            </div>

            <label
              v-else-if="param.type === 'boolean'"
              class="toggle ab-param-toggle"
            >
              <input
                :id="'ab-pa-' + param.key"
                type="checkbox"
                :checked="!!valueOf(param)"
                @change="update(param.key, ($event.target as HTMLInputElement).checked)"
              />
              <span class="toggle-slider"></span>
            </label>

            <textarea
              v-else-if="param.type === 'text'"
              :id="'ab-pa-' + param.key"
              class="ab-param-textarea"
              rows="2"
              :placeholder="placeholderFor(param)"
              :value="(valueOf(param) as string) ?? ''"
              @input="update(param.key, ($event.target as HTMLTextAreaElement).value)"
            ></textarea>

            <input
              v-else
              type="text"
              class="ab-param-select"
              :value="(valueOf(param) as string) ?? ''"
              @input="update(param.key, ($event.target as HTMLInputElement).value)"
            />
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
