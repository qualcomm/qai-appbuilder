<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->
<script setup lang="ts">
/**
 * Settings -> Extensions (design doc §7): list / add / enable / disable /
 * reload / configure / uninstall user extensions.
 *
 * Adding is two-step: the archive or folder is staged and validated by the
 * backend, which returns what the extension would add (tools with their
 * permission group, skills, prompts, MCP servers, whether it runs code); the
 * user confirms before any of its code runs.
 *
 * Settings card: field types follow the manifest (`text` / `secret` / `bool`
 * / `number` / `select`); secrets are write-only (the server only says
 * `is_set`; an empty secret field means "keep").
 */
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";

import {
  confirmExtensionInstall,
  discardExtensionInstall,
  getExtensionSettings,
  listExtensions,
  reloadAllExtensions,
  reloadExtension,
  saveExtensionSettings,
  setExtensionEnabled,
  stageExtensionFolder,
  stageExtensionZip,
  uninstallExtension,
  type ExtensionSetting,
  type ExtensionStatus,
  type InstallSummary,
} from "@/api/extensions";
import { ApiError } from "@/api/errors";
import { useToast } from "@/composables/useToast";

const { t } = useI18n();
const toast = useToast();

const extensions = ref<ExtensionStatus[]>([]);
const directory = ref("");
const loading = ref(false);
const busy = ref<string | null>(null);
const expanded = ref<Set<string>>(new Set());

function errText(err: unknown): string {
  if (err instanceof ApiError) {
    const fields = err.details?.field_errors as Record<string, string[]> | undefined;
    if (fields && Object.keys(fields).length > 0) {
      return Object.entries(fields)
        .map(([k, v]) => `${k}: ${v.join(", ")}`)
        .join("; ");
    }
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    const r = await listExtensions();
    extensions.value = r.extensions;
    directory.value = r.directory;
  } catch (err) {
    toast.error(t("extensions.toast.loadFailed", { error: errText(err) }));
  } finally {
    loading.value = false;
  }
}

function replaceStatus(status: ExtensionStatus): void {
  const i = extensions.value.findIndex((e) => e.name === status.name);
  if (i >= 0) extensions.value.splice(i, 1, status);
  else extensions.value = [...extensions.value, status].sort((a, b) => a.name.localeCompare(b.name));
}

async function run<T>(key: string, op: () => Promise<T>): Promise<T | undefined> {
  if (busy.value) return undefined;
  busy.value = key;
  try {
    return await op();
  } catch (err) {
    toast.error(errText(err));
    return undefined;
  } finally {
    busy.value = null;
  }
}

function reportStatus(status: ExtensionStatus | undefined, okKey: string): void {
  if (!status) return;
  replaceStatus(status);
  if (status.state === "failed") {
    toast.error(t("extensions.toast.failed", { name: status.name, error: status.error }));
  } else {
    toast.success(t(okKey, { name: status.name }));
  }
}

async function onReloadAll(): Promise<void> {
  const r = await run("*", reloadAllExtensions);
  if (r) {
    extensions.value = r.extensions;
    toast.success(t("extensions.toast.reloadedAll"));
  }
}

async function onReload(ext: ExtensionStatus): Promise<void> {
  reportStatus(await run(ext.name, () => reloadExtension(ext.name)), "extensions.toast.reloaded");
}

async function onToggle(ext: ExtensionStatus): Promise<void> {
  const enable = ext.state === "disabled";
  reportStatus(
    await run(ext.name, () => setExtensionEnabled(ext.name, enable)),
    enable ? "extensions.toast.enabled" : "extensions.toast.disabled",
  );
}

function toggleDetails(name: string): void {
  const next = new Set(expanded.value);
  if (next.has(name)) next.delete(name);
  else next.add(name);
  expanded.value = next;
}

function stateLabel(ext: ExtensionStatus): string {
  if (ext.state === "loaded" && ext.warnings.length > 0) return t("extensions.state.warnings");
  return t(`extensions.state.${ext.state}`);
}

function stateClass(ext: ExtensionStatus): string {
  if (ext.state === "failed") return "ext-dot--err";
  if (ext.state === "disabled") return "ext-dot--off";
  return ext.warnings.length > 0 ? "ext-dot--warn" : "ext-dot--ok";
}

// ── Uninstall ────────────────────────────────────────────────────────────
const removing = ref<ExtensionStatus | null>(null);
const purgeData = ref(false);

function askUninstall(ext: ExtensionStatus): void {
  removing.value = ext;
  purgeData.value = false;
}

async function confirmUninstall(): Promise<void> {
  const ext = removing.value;
  if (!ext) return;
  const done = await run(ext.name, async () => {
    await uninstallExtension(ext.name, purgeData.value);
    return true;
  });
  if (done) {
    extensions.value = extensions.value.filter((e) => e.name !== ext.name);
    toast.success(t("extensions.toast.uninstalled", { name: ext.name }));
  }
  removing.value = null;
}

// ── Settings card ────────────────────────────────────────────────────────
const configuring = ref<string | null>(null);
const fields = ref<ExtensionSetting[]>([]);
const draft = ref<Record<string, unknown>>({});
const clearSecrets = ref<Set<string>>(new Set());

async function openSettings(ext: ExtensionStatus): Promise<void> {
  const loaded = await run(ext.name, () => getExtensionSettings(ext.name));
  if (!loaded) return;
  fields.value = loaded;
  draft.value = Object.fromEntries(
    loaded.map((f) => [f.key, f.type === "secret" ? "" : (f.value ?? (f.type === "bool" ? false : ""))]),
  );
  clearSecrets.value = new Set();
  configuring.value = ext.name;
}

async function saveSettings(): Promise<void> {
  const name = configuring.value;
  if (!name) return;
  const values: Record<string, unknown> = {};
  for (const f of fields.value) {
    const v = draft.value[f.key];
    if (f.type === "secret") {
      if (clearSecrets.value.has(f.key)) values[f.key] = null;
      else if (typeof v === "string" && v !== "") values[f.key] = v; // empty = keep
    } else if (f.type === "number") {
      values[f.key] = v === "" || v === null || v === undefined ? null : Number(v);
    } else if (f.type === "text" || f.type === "select") {
      values[f.key] = v === "" ? null : v;
    } else {
      values[f.key] = v;
    }
  }
  const saved = await run(name, () => saveExtensionSettings(name, values));
  if (saved) {
    configuring.value = null;
    toast.success(t("extensions.toast.settingsSaved", { name }));
    await refresh(); // MCP servers may have started / stopped
  }
}

function toggleClearSecret(key: string): void {
  const next = new Set(clearSecrets.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  clearSecrets.value = next;
}

// ── Add (stage -> confirm) ───────────────────────────────────────────────
const fileInput = ref<HTMLInputElement | null>(null);
const showFolder = ref(false);
const folderPath = ref("");
const staged = ref<InstallSummary | null>(null);

async function onPickZip(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  const summary = await run("+", () => stageExtensionZip(file));
  if (summary) staged.value = summary;
}

async function onStageFolder(): Promise<void> {
  const path = folderPath.value.trim();
  if (!path) return;
  const summary = await run("+", () => stageExtensionFolder(path));
  if (summary) {
    staged.value = summary;
    showFolder.value = false;
    folderPath.value = "";
  }
}

async function confirmInstall(): Promise<void> {
  const s = staged.value;
  if (!s) return;
  const status = await run("+", () => confirmExtensionInstall(s.token, s.replaces));
  staged.value = null;
  reportStatus(status, "extensions.toast.installed");
}

async function cancelInstall(): Promise<void> {
  const s = staged.value;
  staged.value = null;
  if (s) {
    try {
      await discardExtensionInstall(s.token);
    } catch {
      // swept by the backend later
    }
  }
}

const groupLabel = (g: string | null): string =>
  g ? t(`extensions.group.${g}`) : t("extensions.group.none");

const empty = computed(() => !loading.value && extensions.value.length === 0);

onMounted(refresh);
</script>

<template>
  <div
    class="ext-panel"
    data-testid="extensions-panel"
  >
    <header class="ext-panel__head">
      <div>
        <h3 class="ext-panel__title">
          {{ t("extensions.title") }}
        </h3>
        <p class="ext-panel__subtitle">
          {{ t("extensions.subtitle") }}
        </p>
        <p
          v-if="directory"
          class="ext-panel__dir"
          :title="directory"
        >
          {{ t("extensions.directory") }} <code>{{ directory }}</code>
        </p>
      </div>
      <div class="ext-panel__actions">
        <input
          ref="fileInput"
          type="file"
          accept=".zip,application/zip"
          class="ext-hidden"
          data-testid="ext-zip-input"
          @change="onPickZip"
        />
        <button
          type="button"
          class="btn btn-primary btn-sm"
          :disabled="!!busy"
          @click="fileInput?.click()"
        >
          {{ t("extensions.action.addZip") }}
        </button>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="!!busy"
          @click="showFolder = !showFolder"
        >
          {{ t("extensions.action.addFolder") }}
        </button>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="!!busy"
          @click="onReloadAll"
        >
          {{ t("extensions.action.reloadAll") }}
        </button>
      </div>
    </header>

    <form
      v-if="showFolder"
      class="ext-folder"
      @submit.prevent="onStageFolder"
    >
      <input
        v-model="folderPath"
        class="input"
        :placeholder="t('extensions.folderPlaceholder')"
        data-testid="ext-folder-input"
      />
      <button
        type="submit"
        class="btn btn-primary btn-sm"
        :disabled="!folderPath.trim() || !!busy"
      >
        {{ t("extensions.action.next") }}
      </button>
    </form>

    <p
      v-if="loading && extensions.length === 0"
      class="ext-panel__muted"
    >
      {{ t("common.loading") }}
    </p>
    <div
      v-else-if="empty"
      class="ext-panel__empty"
    >
      <p>{{ t("extensions.empty.title") }}</p>
      <p class="ext-panel__muted">
        {{ t("extensions.empty.hint") }}
      </p>
    </div>

    <ul
      v-else
      class="ext-list"
    >
      <li
        v-for="ext in extensions"
        :key="ext.name"
        class="ext-card"
        :class="{ 'ext-card--off': ext.state === 'disabled' }"
        :data-testid="`ext-card-${ext.name}`"
      >
        <div class="ext-card__main">
          <span
            class="ext-dot"
            :class="stateClass(ext)"
          />
          <div class="ext-card__info">
            <div class="ext-card__name-row">
              <span class="ext-card__name">{{ ext.name }}</span>
              <span
                v-if="ext.version"
                class="ext-badge"
              >v{{ ext.version }}</span>
              <span
                v-if="ext.builtin"
                class="ext-badge ext-badge--builtin"
                :data-testid="`ext-builtin-${ext.name}`"
                :title="t('extensions.builtinHint')"
              >{{ t("extensions.builtin") }}</span>
              <span
                class="ext-badge"
                :class="`ext-badge--${ext.state}`"
              >{{ stateLabel(ext) }}</span>
            </div>
            <div class="ext-card__summary">
              <span v-if="ext.tools.length">{{ t("extensions.count.tools", { n: ext.tools.length }) }}</span>
              <span v-if="ext.skills.length">{{ t("extensions.count.skills", { n: ext.skills.length }) }}</span>
              <span v-if="ext.mcp_servers.length">{{ t("extensions.count.mcp", { n: ext.mcp_servers.length }) }}</span>
              <span v-if="Object.keys(ext.prompts).length">{{ t("extensions.count.prompts") }}</span>
            </div>
            <p
              v-if="ext.error"
              class="ext-card__error"
            >
              {{ ext.error }}
            </p>
            <div
              v-if="expanded.has(ext.name)"
              class="ext-card__details"
            >
              <div v-if="ext.tools.length">
                <b>{{ t("extensions.detail.tools") }}</b> {{ ext.tools.join(", ") }}
              </div>
              <div v-if="ext.skills.length">
                <b>{{ t("extensions.detail.skills") }}</b> {{ ext.skills.join(", ") }}
              </div>
              <div v-if="ext.mcp_servers.length">
                <b>{{ t("extensions.detail.mcp") }}</b> {{ ext.mcp_servers.join(", ") }}
              </div>
              <ul
                v-if="ext.warnings.length"
                class="ext-card__warnings"
              >
                <li
                  v-for="(w, i) in ext.warnings"
                  :key="i"
                >
                  {{ w }}
                </li>
              </ul>
            </div>
          </div>
        </div>
        <div class="ext-card__actions">
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            @click="toggleDetails(ext.name)"
          >
            {{ expanded.has(ext.name) ? t("extensions.action.less") : t("extensions.action.details") }}
          </button>
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            :disabled="!!busy"
            :data-testid="`ext-settings-${ext.name}`"
            @click="openSettings(ext)"
          >
            {{ t("extensions.action.settings") }}
          </button>
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            :disabled="!!busy"
            :data-testid="`ext-toggle-${ext.name}`"
            @click="onToggle(ext)"
          >
            {{ ext.state === "disabled" ? t("extensions.action.enable") : t("extensions.action.disable") }}
          </button>
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            :disabled="!!busy || ext.state === 'disabled'"
            @click="onReload(ext)"
          >
            {{ busy === ext.name ? t("extensions.action.working") : t("extensions.action.reload") }}
          </button>
          <button
            v-if="!ext.builtin"
            type="button"
            class="btn btn-ghost btn-sm ext-danger"
            :disabled="!!busy"
            :data-testid="`ext-remove-${ext.name}`"
            @click="askUninstall(ext)"
          >
            {{ t("extensions.action.uninstall") }}
          </button>
        </div>
      </li>
    </ul>

    <!-- Install confirmation: what the extension adds (nothing ran yet). -->
    <Teleport to="body">
      <div
        v-if="staged"
        class="ext-modal-overlay"
        @click.self="cancelInstall"
      >
        <div
          class="ext-modal"
          role="dialog"
          aria-modal="true"
          data-testid="ext-install-dialog"
        >
          <h3 class="ext-modal__title">
            {{ staged.replaces ? t("extensions.install.titleUpdate") : t("extensions.install.title") }}
          </h3>
          <p class="ext-modal__name">
            <b>{{ staged.name }}</b> v{{ staged.version }}
            <span v-if="staged.installed_version"> ({{ t("extensions.install.replaces", { v: staged.installed_version }) }})</span>
          </p>
          <p class="ext-panel__muted">
            {{ staged.description }}
          </p>
          <p
            v-if="staged.runs_code"
            class="ext-modal__warn"
          >
            {{ t("extensions.install.runsCode") }}
          </p>
          <p
            v-if="staged.runs_frontend_code"
            class="ext-modal__warn"
            data-testid="ext-install-frontend-warning"
          >
            {{ t("extensions.install.runsFrontendCode") }}
          </p>
          <dl class="ext-modal__caps">
            <template v-if="staged.tools.length">
              <dt>{{ t("extensions.detail.tools") }}</dt>
              <dd>
                <span
                  v-for="tool in staged.tools"
                  :key="tool.name"
                  class="ext-chip"
                >
                  {{ tool.name }} · {{ groupLabel(tool.group) }}
                </span>
              </dd>
            </template>
            <template v-if="staged.skills.length">
              <dt>{{ t("extensions.detail.skills") }}</dt>
              <dd>{{ staged.skills.join(", ") }}</dd>
            </template>
            <template v-if="staged.mcp_servers.length">
              <dt>{{ t("extensions.detail.mcp") }}</dt>
              <dd>
                <div
                  v-for="m in staged.mcp_servers"
                  :key="m.name"
                >
                  {{ m.name }} — <code>{{ m.command || m.url }}</code>
                </div>
              </dd>
            </template>
            <template v-if="staged.prompts.length">
              <dt>{{ t("extensions.detail.prompts") }}</dt>
              <dd>{{ staged.prompts.join(", ") }}</dd>
            </template>
            <template v-if="staged.settings.length">
              <dt>{{ t("extensions.detail.settings") }}</dt>
              <dd>{{ staged.settings.map((s) => s.label).join(", ") }}</dd>
            </template>
          </dl>
          <ul
            v-if="staged.warnings.length"
            class="ext-card__warnings"
          >
            <li
              v-for="(w, i) in staged.warnings"
              :key="i"
            >
              {{ w }}
            </li>
          </ul>
          <p class="ext-panel__muted">
            {{ t("extensions.install.trust") }}
          </p>
          <div class="ext-modal__buttons">
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              @click="cancelInstall"
            >
              {{ t("common.cancel") }}
            </button>
            <button
              type="button"
              class="btn btn-primary btn-sm"
              :disabled="!!busy"
              data-testid="ext-install-confirm"
              @click="confirmInstall"
            >
              {{ staged.replaces ? t("extensions.action.update") : t("extensions.action.install") }}
            </button>
          </div>
        </div>
      </div>

      <!-- Settings card -->
      <div
        v-if="configuring"
        class="ext-modal-overlay"
        @click.self="configuring = null"
      >
        <form
          class="ext-modal"
          role="dialog"
          aria-modal="true"
          data-testid="ext-settings-dialog"
          @submit.prevent="saveSettings"
        >
          <h3 class="ext-modal__title">
            {{ t("extensions.settings.title", { name: configuring }) }}
          </h3>
          <p
            v-if="fields.length === 0"
            class="ext-panel__muted"
          >
            {{ t("extensions.settings.none") }}
          </p>
          <label
            v-for="f in fields"
            :key="f.key"
            class="ext-field"
          >
            <span class="ext-field__label">
              {{ f.label }}<span
                v-if="f.required"
                class="ext-field__req"
              >*</span>
            </span>
            <input
              v-if="f.type === 'bool'"
              v-model="draft[f.key]"
              type="checkbox"
            />
            <select
              v-else-if="f.type === 'select'"
              v-model="draft[f.key]"
              class="input"
            >
              <option value="">—</option>
              <option
                v-for="o in f.options"
                :key="o"
                :value="o"
              >{{ o }}</option>
            </select>
            <input
              v-else-if="f.type === 'number'"
              v-model="draft[f.key]"
              type="number"
              class="input"
              :min="f.min ?? undefined"
              :max="f.max ?? undefined"
            />
            <template v-else-if="f.type === 'secret'">
              <input
                v-model="draft[f.key]"
                type="password"
                class="input"
                autocomplete="new-password"
                :disabled="clearSecrets.has(f.key)"
                :placeholder="f.is_set ? t('extensions.settings.secretKeep') : t('extensions.settings.secretUnset')"
                :data-testid="`ext-secret-${f.key}`"
              />
              <button
                v-if="f.is_set"
                type="button"
                class="btn btn-ghost btn-sm"
                @click="toggleClearSecret(f.key)"
              >
                {{ clearSecrets.has(f.key) ? t("extensions.settings.keep") : t("extensions.settings.clear") }}
              </button>
            </template>
            <input
              v-else
              v-model="draft[f.key]"
              type="text"
              class="input"
            />
            <span
              v-if="f.description"
              class="ext-field__hint"
            >{{ f.description }}</span>
          </label>
          <div class="ext-modal__buttons">
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              @click="configuring = null"
            >
              {{ t("common.cancel") }}
            </button>
            <button
              v-if="fields.length"
              type="submit"
              class="btn btn-primary btn-sm"
              :disabled="!!busy"
              data-testid="ext-settings-save"
            >
              {{ t("common.save") }}
            </button>
          </div>
        </form>
      </div>

      <!-- Uninstall -->
      <div
        v-if="removing"
        class="ext-modal-overlay"
        @click.self="removing = null"
      >
        <div
          class="ext-modal"
          role="dialog"
          aria-modal="true"
          data-testid="ext-uninstall-dialog"
        >
          <h3 class="ext-modal__title">
            {{ t("extensions.uninstall.title", { name: removing.name }) }}
          </h3>
          <p>{{ t("extensions.uninstall.message") }}</p>
          <label class="ext-check">
            <input
              v-model="purgeData"
              type="checkbox"
              data-testid="ext-purge"
            />
            {{ t("extensions.uninstall.purge") }}
          </label>
          <div class="ext-modal__buttons">
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              @click="removing = null"
            >
              {{ t("common.cancel") }}
            </button>
            <button
              type="button"
              class="btn btn-danger btn-sm"
              :disabled="!!busy"
              data-testid="ext-uninstall-confirm"
              @click="confirmUninstall"
            >
              {{ t("extensions.action.uninstall") }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style>
.ext-panel { display: flex; flex-direction: column; gap: var(--space-4); }
.ext-panel__head { display: flex; justify-content: space-between; gap: var(--space-4); flex-wrap: wrap; }
.ext-panel__title { margin: 0; font-size: var(--text-lg); color: var(--text-primary); }
.ext-panel__subtitle, .ext-panel__muted, .ext-panel__dir { margin: var(--space-1) 0 0; font-size: var(--text-sm); color: var(--text-secondary); }
.ext-panel__dir code { font-size: var(--text-xs); }
.ext-panel__actions { display: flex; gap: var(--space-2); align-items: flex-start; flex-wrap: wrap; }
.ext-panel__empty { padding: var(--space-6); text-align: center; border: 1px dashed var(--border); border-radius: var(--radius-md, 8px); }
.ext-hidden { display: none; }
.ext-folder { display: flex; gap: var(--space-2); }
.ext-folder .input { flex: 1; }
.ext-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
.ext-card { display: flex; justify-content: space-between; gap: var(--space-3); padding: var(--space-3); border: 1px solid var(--border); border-radius: var(--radius-md, 8px); background: var(--bg-secondary); flex-wrap: wrap; }
.ext-card--off { opacity: 0.65; }
.ext-card__main { display: flex; gap: var(--space-3); min-width: 0; flex: 1; }
.ext-card__info { min-width: 0; flex: 1; }
.ext-card__name-row { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
.ext-card__name { font-weight: 600; color: var(--text-primary); }
.ext-card__summary { display: flex; gap: var(--space-3); font-size: var(--text-sm); color: var(--text-secondary); margin-top: var(--space-1); }
.ext-card__error { color: var(--error); font-size: var(--text-sm); margin: var(--space-1) 0 0; white-space: pre-wrap; }
.ext-card__details { margin-top: var(--space-2); font-size: var(--text-sm); display: flex; flex-direction: column; gap: var(--space-1); }
.ext-card__warnings { margin: var(--space-1) 0 0; padding-left: var(--space-5); color: var(--warning, #b7791f); font-size: var(--text-sm); }
.ext-card__actions { display: flex; gap: var(--space-1); align-items: flex-start; flex-wrap: wrap; }
.ext-danger { color: var(--error); }
.ext-dot { width: 10px; height: 10px; border-radius: 50%; margin-top: 6px; flex: 0 0 auto; background: var(--text-secondary); }
.ext-dot--ok { background: var(--success, #2f9e44); }
.ext-dot--warn { background: var(--warning, #f59f00); }
.ext-dot--err { background: var(--error); }
.ext-badge { font-size: var(--text-xs); padding: 0 var(--space-2); border: 1px solid var(--border); border-radius: var(--radius-sm, 4px); color: var(--text-secondary); }
.ext-badge--failed { color: var(--error); border-color: var(--error); }
.ext-badge--builtin { color: var(--accent); border-color: var(--accent); }
.ext-chip { display: inline-block; margin: 0 var(--space-1) var(--space-1) 0; padding: 0 var(--space-2); border-radius: var(--radius-sm, 4px); background: var(--bg-tertiary, var(--bg-primary)); font-size: var(--text-xs); }
.ext-check { display: flex; align-items: center; gap: var(--space-2); font-size: var(--text-sm); }
</style>
