<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * MaskedPasswordInput — V1-parity API-key / secret input with eye toggle.
 *
 * V1 (``ServiceConfigPanel.js:305-312`` and ``:423-430``) renders API-key
 * fields as a 🔑-prefixed ``<input type="password">`` next to a
 * ``svc-cfg-eye-btn`` that flips between hidden ("•••••") and revealed
 * (raw text) display so operators can verify the pasted key. The visuals
 * (``.svc-cfg-input-wrap`` / ``.svc-cfg-input`` /
 * ``.svc-cfg-eye-btn``) already live in ``service-config.css`` and use
 * global ``var(--*)`` tokens — no hard-coded colours here.
 *
 * The "treat ``****`` as unchanged" draft policy that V1 / V2's CloudTab * use is **not** baked into this component: it varies per call-site * (cloud_model.api_key vs enterprise_cloud_model.api_key vs other secret * fields) and depends on the parent's save flow. This component is a
 * dumb, controlled input — the parent owns the draft / mask semantics
 * and v-models the draft string here. Single responsibility: render a
 * masked text input + an eye button that toggles its visibility.
 *
 * Props:
 *   • modelValue (string)         — current draft text; v-model
 *   • prefixIcon (string?)        — emoji rendered before the input (V1: 🔑)
 *   • placeholder / disabled / aria-label / id — passed through to <input>
 */
import { ref } from "vue";

defineProps<{
  modelValue: string;
  prefixIcon?: string;
  placeholder?: string;
  disabled?: boolean;
  ariaLabel?: string;
  inputId?: string;
}>();

defineEmits<{ (e: "update:modelValue", value: string): void }>();

const visible = ref(false);
function toggleVisible(): void {
  visible.value = !visible.value;
}
</script>

<template>
  <div class="svc-cfg-input-wrap masked-pwd">
    <span
      v-if="prefixIcon"
      class="masked-pwd__prefix"
      aria-hidden="true"
    >{{ prefixIcon }}</span>
    <input
      :id="inputId"
      class="svc-cfg-input"
      :class="{ 'masked-pwd__input--with-prefix': !!prefixIcon }"
      :type="visible ? 'text' : 'password'"
      :value="modelValue"
      :placeholder="placeholder"
      :disabled="disabled"
      :aria-label="ariaLabel"
      autocomplete="off"
      @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    />
    <button
      type="button"
      class="svc-cfg-eye-btn"
      :aria-label="visible ? 'Hide' : 'Show'"
      :aria-pressed="visible"
      :disabled="disabled"
      @click="toggleVisible"
    >
      {{ visible ? "🙈" : "👁" }}
    </button>
  </div>
</template>

<style scoped>
.masked-pwd {
  position: relative;
}
.masked-pwd__prefix {
  position: absolute;
  left: 8px;
  top: 50%;
  transform: translateY(-50%);
  font-size: var(--text-md);
  pointer-events: none;
  color: var(--text-muted);
}
.masked-pwd__input--with-prefix {
  padding-left: 28px;
}
</style>
