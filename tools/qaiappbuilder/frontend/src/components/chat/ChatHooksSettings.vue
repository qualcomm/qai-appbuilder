<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChatHooksSettings — manages "chat action hooks": shell commands the AI
 * runs automatically at a given event point (e.g. "run lint after editing
 * code"). Backed by a set of settings endpoints.
 *
 * Backend contract (fixed):
 *   GET  /api/settings/chat_hooks
 *     → { hooks: [{ event, command, timeout_s }, ...] }
 *   PUT  /api/settings/chat_hooks  body { hooks: [...] }
 *     → returns the saved { hooks: [...] }
 *   GET  /api/settings/chat_hooks_enabled       → { enabled: boolean }
 *   PUT  /api/settings/chat_hooks_enabled  body { enabled }
 *     → { enabled: boolean }
 *
 * The 10 valid `event` values are surfaced as a localized dropdown.
 *
 * Edit model: the whole hooks list is edited locally (add / edit fields
 * inline / delete) and persisted in one PUT via the "Save" button — matching
 * the settings endpoint shape (the backend stores the full array). The master
 * enable toggle persists immediately on change (single-value setting, no batch
 * Save semantics).
 *
 * NOTE: per-profile sub-agent model overrides used to live here too; they were
 * moved to the dedicated 🤝 Agent tab (AgentSettingsPanel.vue) so this tab is
 * purely about chat action hooks.
 */
import { ref, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson } from "@/api";
import { useToast } from "@/composables/useToast";
import { useConfirm } from "@/composables/useConfirm";

// ─── Types (mirror the fixed backend contract) ───────────────────────────────

const HOOK_EVENTS = [
  "pre_tool_call",
  "post_tool_call",
  "pre_message",
  "post_message",
  "on_error",
  "on_complete",
  "on_user_input",
  "on_session_start",
  "on_session_end",
  "on_truncate",
] as const;

type HookEvent = (typeof HOOK_EVENTS)[number];

interface ChatHook {
  event: HookEvent;
  command: string;
  timeout_s: number;
}

interface ChatHooksResponse {
  hooks: ChatHook[];
}

interface HooksEnabledResponse {
  enabled: boolean;
}

const DEFAULT_TIMEOUT_S = 30;

// ─── State ───────────────────────────────────────────────────────────────────

const { t } = useI18n();
const toast = useToast();
const { confirm } = useConfirm();

const hooks = ref<ChatHook[]>([]);
const loading = ref(false);
const saving = ref(false);

// Master enable toggle (arbitrary command execution gate).
const enabled = ref(false);
const enabledLoading = ref(false);
const enabledSaving = ref(false);

// Interceptor docs disclosure.
const docsOpen = ref(false);

// ─── Load ────────────────────────────────────────────────────────────────────

async function loadHooks(): Promise<void> {
  loading.value = true;
  try {
    const res = await apiJson<ChatHooksResponse>(
      "GET",
      "/api/settings/chat_hooks",
    );
    hooks.value = normalize(res.hooks);
  } catch {
    hooks.value = [];
    toast.error(t("chatHooks.loadFailed"));
  } finally {
    loading.value = false;
  }
}

async function loadEnabled(): Promise<void> {
  enabledLoading.value = true;
  try {
    const res = await apiJson<HooksEnabledResponse>(
      "GET",
      "/api/settings/chat_hooks_enabled",
    );
    enabled.value = res.enabled === true;
  } catch {
    enabled.value = false;
    toast.error(t("chatHooks.enable.loadFailed"));
  } finally {
    enabledLoading.value = false;
  }
}

function normalize(raw: ChatHook[] | undefined): ChatHook[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((h) => ({
    event: (HOOK_EVENTS as readonly string[]).includes(h.event)
      ? h.event
      : "pre_tool_call",
    command: typeof h.command === "string" ? h.command : "",
    timeout_s:
      typeof h.timeout_s === "number" && Number.isFinite(h.timeout_s)
        ? h.timeout_s
        : DEFAULT_TIMEOUT_S,
  }));
}

// ─── Mutations (local; persisted on Save) ─────────────────────────────────────

function addHook(): void {
  hooks.value = [
    ...hooks.value,
    { event: "pre_tool_call", command: "", timeout_s: DEFAULT_TIMEOUT_S },
  ];
}

async function deleteHook(index: number): Promise<void> {
  const ok = await confirm({
    icon: "🗑️",
    title: t("chatHooks.confirm.deleteTitle"),
    message: t("chatHooks.confirm.deleteMessage"),
    confirmText: t("chatHooks.confirm.deleteConfirm"),
    cancelText: t("chatHooks.confirm.cancel"),
    confirmStyle: "danger",
  });
  if (!ok) return;
  hooks.value = hooks.value.filter((_, i) => i !== index);
}

function onTimeoutInput(index: number, ev: Event): void {
  const n = Number((ev.target as HTMLInputElement).value);
  const row = hooks.value[index];
  if (row === undefined) return;
  row.timeout_s = Number.isFinite(n) && n > 0 ? Math.round(n) : DEFAULT_TIMEOUT_S;
}

// ─── Save ──────────────────────────────────────────────────────────────────

async function saveHooks(): Promise<void> {
  if (saving.value) return;
  saving.value = true;
  try {
    const payload: ChatHooksResponse = {
      hooks: hooks.value.map((h) => ({
        event: h.event,
        command: h.command,
        timeout_s: h.timeout_s,
      })),
    };
    const res = await apiJson<ChatHooksResponse, ChatHooksResponse>(
      "PUT",
      "/api/settings/chat_hooks",
      payload,
    );
    hooks.value = normalize(res.hooks);
    toast.success(t("chatHooks.saved"));
  } catch (e) {
    toast.error(
      `${t("chatHooks.saveFailed")}: ${e instanceof Error ? e.message : String(e)}`,
    );
  } finally {
    saving.value = false;
  }
}

// ─── Master enable toggle (persists immediately) ──────────────────────────────

async function onToggleEnabled(next: boolean): Promise<void> {
  if (enabledSaving.value) return;
  const previous = enabled.value;
  enabled.value = next;
  enabledSaving.value = true;
  try {
    const res = await apiJson<HooksEnabledResponse, HooksEnabledResponse>(
      "PUT",
      "/api/settings/chat_hooks_enabled",
      { enabled: next },
    );
    enabled.value = res.enabled === true;
    toast.success(
      enabled.value
        ? t("chatHooks.enable.savedOn")
        : t("chatHooks.enable.savedOff"),
    );
  } catch (e) {
    enabled.value = previous;
    toast.error(
      `${t("chatHooks.enable.saveFailed")}: ${e instanceof Error ? e.message : String(e)}`,
    );
  } finally {
    enabledSaving.value = false;
  }
}

onMounted(() => {
  void loadHooks();
  void loadEnabled();
});
</script>

<template>
  <div class="chat-hooks-settings">
    <header class="chat-hooks-header">
      <h3 class="chat-hooks-title">
        {{ t("chatHooks.title") }}
      </h3>
      <p class="chat-hooks-subtitle">
        {{ t("chatHooks.subtitle") }}
      </p>
    </header>

    <!-- Master enable toggle (arbitrary command execution gate) -->
    <div class="chat-hooks-enable">
      <div class="chat-hooks-enable-head">
        <label class="chat-hooks-switch">
          <input
            type="checkbox"
            :checked="enabled"
            :disabled="enabledLoading || enabledSaving"
            @change="onToggleEnabled(($event.target as HTMLInputElement).checked)"
          />
          <span class="chat-hooks-switch-label">
            {{ t("chatHooks.enable.label") }}
          </span>
        </label>
      </div>
      <p
        v-if="enabled"
        class="chat-hooks-security-warning"
      >
        ⚠️ {{ t("chatHooks.enable.securityWarning") }}
      </p>
      <p
        v-else
        class="chat-hooks-disabled-hint"
      >
        {{ t("chatHooks.enable.disabledHint") }}
      </p>
    </div>

    <!-- Interceptor documentation (pre_tool_call stdout JSON steering) -->
    <details
      class="chat-hooks-docs"
      :open="docsOpen"
      @toggle="docsOpen = ($event.target as HTMLDetailsElement).open"
    >
      <summary class="chat-hooks-docs-summary">
        {{ t("chatHooks.docs.title") }}
      </summary>
      <div class="chat-hooks-docs-body">
        <p class="chat-hooks-docs-intro">
          {{ t("chatHooks.docs.intro") }}
        </p>
        <ul class="chat-hooks-docs-list">
          <li>
            <code>{"decision":"deny","reason":"…"}</code>
            — {{ t("chatHooks.docs.deny") }}
          </li>
          <li>
            <code>{"decision":"allow"}</code>
            — {{ t("chatHooks.docs.allow") }}
          </li>
          <li>
            <code>{"updated_input":{…}}</code>
            — {{ t("chatHooks.docs.updatedInput") }}
          </li>
          <li>
            <code>{"additional_context":"…"}</code>
            — {{ t("chatHooks.docs.additionalContext") }}
          </li>
        </ul>
        <p class="chat-hooks-docs-observer">
          {{ t("chatHooks.docs.observer") }}
        </p>
        <p class="chat-hooks-docs-example-label">
          {{ t("chatHooks.docs.exampleLabel") }}
        </p>
        <pre class="chat-hooks-docs-example"><code>echo '{"decision":"deny","reason":"rm blocked"}'</code></pre>
      </div>
    </details>

    <div class="chat-hooks-list">
      <p
        v-if="!loading && hooks.length === 0"
        class="chat-hooks-empty"
      >
        {{ t("chatHooks.empty") }}
      </p>

      <div
        v-for="(hook, index) in hooks"
        :key="index"
        class="chat-hooks-row"
      >
        <div class="config-field chat-hooks-field-event">
          <label class="config-label">{{ t("chatHooks.field.event") }}</label>
          <select
            v-model="hook.event"
            class="config-input"
          >
            <option
              v-for="ev in HOOK_EVENTS"
              :key="ev"
              :value="ev"
            >
              {{ t(`chatHooks.event.${ev}`) }}
            </option>
          </select>
        </div>

        <div class="config-field chat-hooks-field-command">
          <label class="config-label">{{ t("chatHooks.field.command") }}</label>
          <input
            v-model="hook.command"
            class="config-input mono"
            :placeholder="t('chatHooks.placeholder.command')"
          />
        </div>

        <div class="config-field chat-hooks-field-timeout">
          <label class="config-label">{{ t("chatHooks.field.timeout") }}</label>
          <input
            class="config-input"
            type="number"
            min="1"
            :value="hook.timeout_s"
            @input="onTimeoutInput(index, $event)"
          />
        </div>

        <button
          type="button"
          class="btn btn-ghost btn-sm chat-hooks-delete"
          :title="t('chatHooks.action.delete')"
          @click="deleteHook(index)"
        >
          🗑️
        </button>
      </div>
    </div>

    <div class="chat-hooks-actions">
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        @click="addHook"
      >
        + {{ t("chatHooks.action.add") }}
      </button>
      <button
        type="button"
        class="btn btn-primary btn-sm"
        :disabled="saving"
        @click="saveHooks"
      >
        {{ saving ? t("chatHooks.action.saving") : t("chatHooks.action.save") }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.chat-hooks-settings {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.chat-hooks-header {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.chat-hooks-title {
  font-size: var(--text-lg);
  font-weight: 700;
  margin: 0;
  color: var(--text-primary);
}
.chat-hooks-subtitle {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin: 0;
}
.chat-hooks-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.chat-hooks-empty {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin: 0;
}
.chat-hooks-row {
  display: flex;
  align-items: flex-end;
  gap: var(--space-3);
}
.chat-hooks-field-event {
  flex: 0 0 200px;
}
.chat-hooks-field-command {
  flex: 1 1 auto;
  min-width: 160px;
}
.chat-hooks-field-timeout {
  flex: 0 0 120px;
}
.chat-hooks-delete {
  flex: 0 0 auto;
  color: var(--error);
}
.chat-hooks-actions {
  display: flex;
  gap: var(--space-3);
}

/* ── Master enable toggle ─────────────────────────────────────────────── */
.chat-hooks-enable {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--border-subtle, var(--border));
  border-radius: var(--radius-md, 8px);
  background: var(--surface-2, var(--bg-secondary));
}
.chat-hooks-enable-head {
  display: flex;
  align-items: center;
}
.chat-hooks-switch {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
}
.chat-hooks-switch-label {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-primary);
}
.chat-hooks-security-warning {
  font-size: var(--text-sm);
  color: var(--warning, var(--error));
  margin: 0;
  line-height: 1.4;
}
.chat-hooks-disabled-hint {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin: 0;
}

/* ── Interceptor docs ─────────────────────────────────────────────────── */
.chat-hooks-docs {
  border: 1px solid var(--border-subtle, var(--border));
  border-radius: var(--radius-md, 8px);
  padding: var(--space-2) var(--space-3);
  background: var(--surface-2, var(--bg-secondary));
}
.chat-hooks-docs-summary {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-primary);
  cursor: pointer;
}
.chat-hooks-docs-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-2);
}
.chat-hooks-docs-intro,
.chat-hooks-docs-observer,
.chat-hooks-docs-example-label {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin: 0;
}
.chat-hooks-docs-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding-left: var(--space-4);
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.chat-hooks-docs-list code,
.chat-hooks-docs-example code {
  font-family: var(--font-mono, monospace);
  color: var(--text-primary);
}
.chat-hooks-docs-example {
  margin: 0;
  padding: var(--space-2);
  border-radius: var(--radius-sm, 4px);
  background: var(--surface-3, var(--bg-tertiary, var(--bg)));
  overflow-x: auto;
  font-size: var(--text-sm);
}
</style>
