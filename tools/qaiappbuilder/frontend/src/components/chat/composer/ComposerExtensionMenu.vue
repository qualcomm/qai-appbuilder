<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->
<script setup lang="ts">
/**
 * Composer "Extension ▾" entry (design doc §3.1-§3.3, route B).
 *
 * - No extension selected: an "Extension ▾" button; its menu lists the
 *   loaded extensions that declare `ui`, plus "Extension settings…".
 * - Selected: a mode tag "<icon> <label> ✕" followed by the extension's
 *   action buttons. ✕ leaves the mode. The conversation is never switched
 *   or cleared.
 * - An action button opens a modal generated from the action's fields
 *   (same field types as the settings card; secrets are never prefilled).
 *   On submit the backend runs the extension's handler; its `message` is
 *   shown, and its optional `prompt` is sent as the next chat message
 *   (`send-prompt` event).
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";

import { ApiError } from "@/api/errors";
import { useToast } from "@/composables/useToast";
import {
  useExtensionModes,
  type ExtensionUiAction,
} from "@/composables/useExtensionModes";

const props = defineProps<{
  tabId: string | null;
  conversationId: string | null;
}>();
const emit = defineEmits<{
  "send-prompt": [prompt: string];
  /** An extension mode was entered: the parent leaves any built-in mode. */
  entered: [name: string];
}>();

const { t } = useI18n();
const router = useRouter();
const toast = useToast();
const modes = useExtensionModes();

const open = ref(false);
const root = ref<HTMLElement | null>(null);
const current = computed(() => modes.selectedFor(props.tabId));

async function toggleMenu(): Promise<void> {
  open.value = !open.value;
  if (open.value) await modes.refreshEntries();
}

function pick(name: string): void {
  if (props.tabId) modes.select(props.tabId, name);
  open.value = false;
  emit("entered", name);
}

function exitMode(): void {
  if (props.tabId) modes.clear(props.tabId);
}

function openSettings(): void {
  open.value = false;
  void router.push({ path: "/settings", query: { tab: "extensions" } });
}

function onDocClick(e: MouseEvent): void {
  if (open.value && root.value && !root.value.contains(e.target as Node)) open.value = false;
}
onMounted(() => {
  document.addEventListener("mousedown", onDocClick);
  if (!modes.loaded.value) void modes.refreshEntries();
});
onBeforeUnmount(() => document.removeEventListener("mousedown", onDocClick));

// ── Action dialog ─────────────────────────────────────────────────────────
const action = ref<ExtensionUiAction | null>(null);
const values = ref<Record<string, unknown>>({});
const fieldErrors = ref<Record<string, string>>({});
const running = ref(false);

function openAction(a: ExtensionUiAction): void {
  values.value = Object.fromEntries(
    a.fields.map((f) => [
      f.key,
      f.type === "secret" ? "" : (f.default ?? (f.type === "bool" ? false : "")),
    ]),
  );
  fieldErrors.value = {};
  action.value = a;
}

async function submit(): Promise<void> {
  const ext = current.value;
  const a = action.value;
  if (!ext || !a || running.value) return;
  const payload: Record<string, unknown> = {};
  for (const f of a.fields) {
    const v = values.value[f.key];
    if (f.type === "number") payload[f.key] = v === "" || v === null ? null : Number(v);
    else payload[f.key] = v === "" ? null : v;
  }
  running.value = true;
  try {
    const r = await modes.runAction(ext.name, a.id, payload, {
      conversationId: props.conversationId,
      tabId: props.tabId,
    });
    if (!r.ok) {
      toast.error(r.message || t("extensions.mode.actionFailed"));
      return;
    }
    action.value = null;
    if (r.message) toast.success(r.message);
    if (r.prompt) emit("send-prompt", r.prompt);
  } catch (err) {
    const fe = err instanceof ApiError ? (err.details?.field_errors as Record<string, string[]> | undefined) : undefined;
    if (fe) {
      fieldErrors.value = Object.fromEntries(Object.entries(fe).map(([k, v]) => [k, v.join(", ")]));
    } else {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  } finally {
    running.value = false;
  }
}
</script>

<template>
  <div ref="root" class="ext-mode" data-testid="ext-mode">
    <template v-if="current">
      <button
        type="button"
        class="rit-mode-badge ext-mode__tag"
        data-testid="ext-mode-exit"
        :title="t('extensions.mode.exit')"
        @click="exitMode"
      >
        <span v-if="current.icon" aria-hidden="true">{{ current.icon }}</span>
        <span>{{ current.label }}</span>
        <span class="ext-mode__x" aria-hidden="true">✕</span>
      </button>
      <span v-if="current.actions.length" class="rit-sep"></span>
      <button
        v-for="a in current.actions"
        :key="a.id"
        type="button"
        class="rit-btn"
        :title="a.description || a.label"
        :data-testid="`ext-action-${a.id}`"
        @click="openAction(a)"
      >
        <span>{{ a.label }}</span>
      </button>
    </template>
    <template v-else>
      <button
        type="button"
        class="rit-btn"
        :class="{ 'rit-btn--active': open }"
        data-testid="ext-menu-btn"
        :aria-expanded="open"
        @click="toggleMenu"
      >
        <span aria-hidden="true">🧩</span>
        <span>{{ t("extensions.mode.button") }}</span>
        <span aria-hidden="true">▾</span>
      </button>
      <div v-if="open" class="ext-mode__menu" role="menu" data-testid="ext-menu">
        <button
          v-for="e in modes.entries.value"
          :key="e.name"
          type="button"
          role="menuitem"
          class="ext-mode__item"
          :title="e.description"
          :data-testid="`ext-menu-item-${e.name}`"
          @click="pick(e.name)"
        >
          <span class="ext-mode__icon" aria-hidden="true">{{ e.icon || "🧩" }}</span>
          <span>{{ e.label }}</span>
        </button>
        <p v-if="modes.entries.value.length === 0" class="ext-mode__empty">
          {{ t("extensions.mode.none") }}
        </p>
        <hr class="ext-mode__sep" />
        <button type="button" role="menuitem" class="ext-mode__item" @click="openSettings">
          {{ t("extensions.mode.settings") }}
        </button>
      </div>
    </template>

    <Teleport to="body">
      <div v-if="action && current" class="ext-modal-overlay" @click.self="action = null">
        <form
          class="ext-modal"
          role="dialog"
          aria-modal="true"
          data-testid="ext-action-dialog"
          @submit.prevent="submit"
          @keydown.esc="action = null"
        >
          <h3 class="ext-modal__title">{{ current.label }} — {{ action.label }}</h3>
          <p v-if="action.description" class="ext-panel__muted">{{ action.description }}</p>
          <label v-for="f in action.fields" :key="f.key" class="ext-field">
            <span class="ext-field__label">
              {{ f.label }}<span v-if="f.required" class="ext-field__req">*</span>
            </span>
            <input v-if="f.type === 'bool'" v-model="values[f.key]" type="checkbox" />
            <select v-else-if="f.type === 'select'" v-model="values[f.key]" class="input">
              <option value="">—</option>
              <option v-for="o in f.options" :key="o" :value="o">{{ o }}</option>
            </select>
            <input
              v-else-if="f.type === 'number'"
              v-model="values[f.key]"
              type="number"
              class="input"
              :min="f.min ?? undefined"
              :max="f.max ?? undefined"
            />
            <input
              v-else
              v-model="values[f.key]"
              :type="f.type === 'secret' ? 'password' : 'text'"
              :autocomplete="f.type === 'secret' ? 'new-password' : 'off'"
              class="input"
              :data-testid="`ext-action-field-${f.key}`"
            />
            <span v-if="f.description" class="ext-field__hint">{{ f.description }}</span>
            <span v-if="fieldErrors[f.key]" class="ext-field__error">{{ fieldErrors[f.key] }}</span>
          </label>
          <div class="ext-modal__buttons">
            <button type="button" class="btn btn-ghost btn-sm" @click="action = null">{{ t("common.cancel") }}</button>
            <button type="submit" class="btn btn-primary btn-sm" :disabled="running" data-testid="ext-action-submit">
              {{ running ? t("extensions.action.working") : t("extensions.mode.run") }}
            </button>
          </div>
        </form>
      </div>
    </Teleport>
  </div>
</template>

<style>
.ext-mode { position: relative; display: inline-flex; align-items: center; gap: 4px; }
.ext-mode__tag { display: inline-flex; align-items: center; gap: 4px; }
.ext-mode__x { opacity: 0.7; margin-left: 2px; }
.ext-mode__menu {
  position: absolute; bottom: calc(100% + 6px); left: 0; z-index: 50; min-width: 220px;
  padding: 4px; border: 1px solid var(--border); border-radius: var(--radius-md, 8px);
  background: var(--bg-secondary); box-shadow: var(--shadow-lg, 0 8px 24px rgba(0, 0, 0, 0.25));
}
.ext-mode__item {
  display: flex; align-items: center; gap: 8px; width: 100%; padding: 6px 10px; border: 0;
  border-radius: var(--radius-sm, 4px); background: transparent; color: var(--text-primary);
  font-size: var(--text-sm); text-align: left; cursor: pointer;
}
.ext-mode__item:hover { background: var(--bg-hover, rgba(127, 127, 127, 0.12)); }
.ext-mode__icon { width: 18px; text-align: center; }
.ext-mode__empty { margin: 4px 10px; font-size: var(--text-xs); color: var(--text-secondary); }
.ext-mode__sep { margin: 4px 0; border: 0; border-top: 1px solid var(--border); }
.ext-field__error { font-size: var(--text-xs); color: var(--error); }
</style>
