<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModeFrameCoding — chat-input sub-toolbar for `code` mode.
 *
 * Four controls (V1 parity, index.html L1964-2133):
 *   1. Persona picker (`rit-code-persona-btn`) — lists personas from
 *      `GET /api/code-personas`; selecting one persists via
 *      `POST /api/code-personas/select` and emits the id up.
 *   2. Speed picker (`fast` / `think` / `expert`) — `rit-submenu-wrap`.
 *   3. Upload code file (`rit-model-upload`) — uploads one code file to
 *      `POST /api/upload/code`; on success emits `update:filePath` with
 *      the server-side absolute path (V1 `codeUploadedPath` →
 *      `tool_params.file_path`).
 *   4. Import open-source repo (`rit-submenu-wrap`) — confirms an
 *      `https?://` repo URL and emits `update:repoUrl` (V1
 *      `codeRepoConfirmed` → `tool_params.repo_url`).
 *
 * All selections are emitted via `update:*` so the parent
 * (ChatComposer) stores them on the active tab's `toolParams` for the
 * transport to forward to the backend.
 */
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import { useCodePersonas } from "@/composables/useCodePersonas";
import { useUpload } from "@/composables/useUpload";
import { useToast } from "@/composables/useToast";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useModeFrameTriggers } from "@/composables/useModeFrameTriggers";

const { t, te } = useI18n();
const toast = useToast();
const router = useRouter();

type CodeSpeed = "fast" | "think" | "expert";

const props = withDefaults(
  defineProps<{
    speed?: CodeSpeed;
    persona?: string | null;
    filePath?: string;
    repoUrl?: string;
    /**
     * V1 parity (app.js:531-538): the persona switcher only applies to
     * cloud models. On-device models hide it entirely. Defaults to `true`
     * so it is never hidden mid-load (matches V1's "default to cloud").
     */
    currentModelIsCloud?: boolean;
  }>(),
  {
    speed: "fast",
    persona: null,
    filePath: "",
    repoUrl: "",
    currentModelIsCloud: true,
  },
);

const emit = defineEmits<{
  exit: [];
  "update:speed": [value: CodeSpeed];
  "update:persona": [value: string | null];
  "update:filePath": [value: string];
  "update:repoUrl": [value: string];
}>();

const submenuOpen = ref(false);
const personaMenuOpen = ref(false);
const speeds: ReadonlyArray<{ id: CodeSpeed; labelKey: string; descKey: string }> = [
  { id: "fast", labelKey: "index.codeFast", descKey: "index.codeFastDesc" },
  { id: "think", labelKey: "index.codeThink", descKey: "index.codeThinkDesc" },
  { id: "expert", labelKey: "index.codeExpert", descKey: "index.codeExpertDesc" },
];

// ── Personas ───────────────────────────────────────────────────────────────
const { personas, activePersona, fetchPersonas, selectPersonaPersisted } =
  useCodePersonas();

/**
 * V1 parity (index.html:1968-1981): the currently-selected persona object,
 * resolved from the upstream `persona` id. Used both for the trigger button's
 * label and for the "customized" red-dot indicator (`is_customized`). A single
 * computed avoids re-scanning `personas` in multiple template/label paths.
 */
const currentPersona = computed(
  () => personas.value.find((p) => p.id === props.persona) ?? null,
);

onMounted(() => {
  void fetchPersonas().then(() => {
    // Seed the upstream persona id from the backend's selected persona
    // if the parent has not chosen one yet.
    if (props.persona === null && activePersona.value !== null) {
      emit("update:persona", activePersona.value.id);
    }
  });
});

function onExit(): void {
  emit("exit");
}

function pickSpeed(id: CodeSpeed): void {
  emit("update:speed", id);
  submenuOpen.value = false;
}

function toggleSubmenu(): void {
  submenuOpen.value = !submenuOpen.value;
  if (submenuOpen.value) personaMenuOpen.value = false;
}

function togglePersonaMenu(): void {
  personaMenuOpen.value = !personaMenuOpen.value;
  if (personaMenuOpen.value) submenuOpen.value = false;
}

function pickPersona(id: string): void {
  emit("update:persona", id);
  void selectPersonaPersisted(id);
  personaMenuOpen.value = false;
}

/**
 * V1 parity (`index.html:2011-2020` `navigateTo('settings','coding-modes')`):
 * close the persona menu and deep-link the user into the Settings page's
 * "Coding Modes" tab where personas can be edited. SettingsView reads
 * `?tab=` to pick the initial tab.
 */
function goEditPersonas(): void {
  personaMenuOpen.value = false;
  void router.push({ path: "/settings", query: { tab: "coding-modes" } });
}

function currentLabel(): string {
  const found = speeds.find((s) => s.id === props.speed);
  return t(found?.labelKey ?? "index.codeFast");
}

/**
 * Localize a persona's name/description (V1 parity:
 * `codePersonas.localizedName/localizedDescription`, useCodePersonas.js:61-80;
 * mirrors CodePersonasPanel.vue:40-51). The backend ships the
 * name/description as hardcoded Chinese (code_personas.py DEFAULT_PERSONAS),
 * so the dropdown previously showed Chinese regardless of UI language. We
 * resolve `codePersona.{id}.name` / `.desc` when present, falling back to the
 * backend value for custom personas with no i18n key.
 */
function localizedPersonaName(p: { id: string; name?: string } | null): string {
  if (!p) return t("index.codePersonaLabel");
  const key = `codePersona.${p.id}.name`;
  return te(key) ? t(key) : (p.name ?? p.id);
}
function localizedPersonaDesc(p: {
  id: string;
  description?: string;
}): string {
  const key = `codePersona.${p.id}.desc`;
  return te(key) ? t(key) : (p.description ?? "");
}

function currentPersonaLabel(): string {
  return localizedPersonaName(currentPersona.value);
}

// ── Upload code file (V1 handleCodeFileSelect, app.js L1288-1319) ────────────
interface CodeUploadResponse {
  path: string;
  filename: string;
  size: number;
}

const { state: uploadState, upload: doUpload } =
  useUpload<CodeUploadResponse>("/api/upload/code");
const codeFileName = ref<string>("");
const isUploading = computed(() => uploadState.value === "uploading");
const codeFileInput = ref<HTMLInputElement | null>(null);

// Extensions accepted by the file picker (V1 index.html:2095).
const CODE_ACCEPT =
  ".py,.js,.ts,.jsx,.tsx,.c,.cpp,.cc,.h,.hpp,.java,.kt,.swift,.go,.rs,.cs," +
  ".rb,.php,.lua,.sh,.bash,.ps1,.sql,.html,.css,.scss,.json,.yaml,.yml," +
  ".toml,.ini,.cfg,.md,.txt,.rst";

async function handleCodeFileSelect(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0] ?? null;
  // Allow re-selecting the same file (V1 resets input.value).
  input.value = "";
  if (file === null) return;

  codeFileName.value = file.name;
  emit("update:filePath", "");

  try {
    const data = await doUpload(file);
    emit("update:filePath", data.path);
    toast.success(t("app.uploadSuccess", { name: file.name }));
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    toast.error(t("app.uploadError", { msg }));
    codeFileName.value = "";
    emit("update:filePath", "");
  }
}

// ── Import open-source repo (V1 confirmCodeRepoUrl, app.js L1325-1349) ────────
const repoInputOpen = ref(false);
const repoUrlDraft = ref<string>("");

// ── Cross-component triggers from ModeIntroCard chips ───────────────────────
// The ModeIntroCard "Pick persona" / "Upload code / repo" chips route through
// `useModeFrameTriggers` — module-level bump tokens we watch here to open
// the corresponding local menus. Only reacts when this frame is actually
// the active mode (mirrors the gate in the other mode-frames).
const _chatTabsStore = useChatTabsStore();
const { openCodePersonaToken, openCodeContextToken } = useModeFrameTriggers();
watch(openCodePersonaToken, () => {
  if (_chatTabsStore.activeTab?.activeMode !== "code") return;
  // Open the persona picker (same effect as clicking the persona button).
  personaMenuOpen.value = true;
});
watch(openCodeContextToken, () => {
  if (_chatTabsStore.activeTab?.activeMode !== "code") return;
  // Open the repo/file URL input row (same effect as clicking the repo button).
  repoInputOpen.value = true;
});

function toggleRepoInput(): void {
  repoInputOpen.value = !repoInputOpen.value;
  if (repoInputOpen.value) {
    submenuOpen.value = false;
    personaMenuOpen.value = false;
    repoUrlDraft.value = props.repoUrl;
  }
}

function confirmRepoUrl(): void {
  const url = repoUrlDraft.value.trim();
  if (!url) {
    emit("update:repoUrl", "");
    repoInputOpen.value = false;
    return;
  }
  // Simple validation: must be an http/https URL (V1 parity).
  if (!/^https?:\/\/.+/.test(url)) {
    toast.warning(t("app.invalidRepoUrl"));
    return;
  }
  emit("update:repoUrl", url);
  repoInputOpen.value = false;
  toast.success(t("app.repoImported", { url }));
}

function clearRepo(): void {
  repoUrlDraft.value = "";
  emit("update:repoUrl", "");
  repoInputOpen.value = false;
}

/** Short repo label (V1 index.html:2111): strip scheme, keep owner/repo. */
function repoShortLabel(): string {
  return props.repoUrl
    .replace(/^https?:\/\//, "")
    .split("/")
    .slice(0, 2)
    .join("/");
}

</script>

<template>
  <div
    class="rit-left"
    data-testid="mode-frame-code"
  >
    <button
      type="button"
      class="rit-mode-badge"
      data-testid="mode-frame-exit"
      @click="onExit"
    >
      <svg
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <polyline points="16 18 22 12 16 6" />
        <polyline points="8 6 2 12 8 18" />
      </svg>
      <span>{{ t("index.coding") }}</span>
      <span class="rit-close">✕</span>
    </button>

    <span class="rit-sep"></span>

    <!-- ── persona 切换器（云端模型专用，V1 index.html:1965） ──
         V1 仅在 currentModelIsCloud 时渲染（本地模型整块隐藏，因端侧
         模型不支持 persona）。ChatComposer 透传 currentModelIsCloud，
         此处用 v-if 对齐 V1 的显隐行为。 -->
    <div
      v-if="props.currentModelIsCloud"
      class="rit-submenu-wrap"
    >
      <button
        type="button"
        class="rit-btn rit-code-persona-btn"
        :class="{ 'rit-code-persona-btn--customized': currentPersona?.is_customized }"
        data-testid="code-persona-trigger"
        :title="t('index.codePersonaTitle')"
        @click="togglePersonaMenu"
      >
        <!-- 通用人形/角色图标（V1 index.html:1973-1977） -->
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
          <circle
            cx="9"
            cy="7"
            r="4"
          />
          <path d="M22 11h-6" />
          <path d="M22 15h-6" />
        </svg>
        <span>{{ currentPersonaLabel() }}</span>
        <!-- 已自定义红点（V1 index.html:1979-1981）：用户改过该 persona 的
             prompt 时（is_customized）在标签后显示一个小圆点。 -->
        <span
          v-if="currentPersona?.is_customized"
          class="rit-code-persona-dot"
          :title="t('codePersona.customizedHint')"
        >●</span>
        <!-- chevron：展开向上、收起向下（V1 index.html:1982-1985） -->
        <svg
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <polyline
            v-if="personaMenuOpen"
            points="18 15 12 9 6 15"
          />
          <polyline
            v-else
            points="6 9 12 15 18 9"
          />
        </svg>
      </button>
      <div
        v-if="personaMenuOpen"
        class="rit-submenu rit-code-persona-menu"
        role="menu"
      >
        <div class="rit-submenu-header">
          {{ t("index.codePersonaHeader") }}
        </div>
        <div
          v-for="p in personas"
          :key="p.id"
          class="rit-submenu-item"
          :class="{ active: props.persona === p.id }"
          :data-testid="`code-persona-${p.id}`"
          role="menuitem"
          @click="pickPersona(p.id)"
        >
          <div class="rit-submenu-item-body">
            <div class="rit-submenu-item-label">
              <!-- A distinct monochrome icon per coding role, keyed on the
                   persona ID (the backend `icon` field is an emoji, so the
                   old `p.icon === 'architect'` checks never matched and every
                   role fell back to the generic `<>` — a latent bug shared
                   with V1). Each built-in role now has its own line icon;
                   custom personas fall back to the generic code glyph. -->
              <!-- code: 编码实现 — code brackets -->
              <svg
                v-if="p.id === 'code'"
                class="rit-persona-icon"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              ><polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" /></svg>
              <!-- architect: 方案规划 — building -->
              <svg
                v-else-if="p.id === 'architect'"
                class="rit-persona-icon"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              ><path d="M3 21h18" /><path d="M5 21V7l7-4 7 4v14" /><path d="M9 9h6" /><path d="M9 13h6" /><path d="M9 17h6" /></svg>
              <!-- ask: 答疑解释 — help circle -->
              <svg
                v-else-if="p.id === 'ask'"
                class="rit-persona-icon"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              ><circle
                cx="12"
                cy="12"
                r="10"
              /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line
                x1="12"
                y1="17"
                x2="12.01"
                y2="17"
              /></svg>
              <!-- reviewer: 代码审查 — magnifier -->
              <svg
                v-else-if="p.id === 'reviewer'"
                class="rit-persona-icon"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              ><circle
                cx="11"
                cy="11"
                r="7"
              /><line
                x1="21"
                y1="21"
                x2="16.65"
                y2="16.65"
              /></svg>
              <!-- debugger: 排错诊断 — bug -->
              <svg
                v-else-if="p.id === 'debugger'"
                class="rit-persona-icon"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              ><rect
                x="8"
                y="6"
                width="8"
                height="14"
                rx="4"
              /><path d="M19 7l-3 2" /><path d="M5 7l3 2" /><path d="M19 13h-3" /><path d="M5 13h3" /><path d="M19 19l-3-2" /><path d="M5 19l3-2" /></svg>
              <!-- optimizer: 重构优化 — lightning -->
              <svg
                v-else-if="p.id === 'optimizer'"
                class="rit-persona-icon"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              ><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></svg>
              <!-- orchestrator: 任务协调 — share/branch nodes -->
              <svg
                v-else-if="p.id === 'orchestrator'"
                class="rit-persona-icon"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              ><circle
                cx="6"
                cy="6"
                r="3"
              /><circle
                cx="18"
                cy="6"
                r="3"
              /><circle
                cx="12"
                cy="18"
                r="3"
              /><path d="M9 6h6" /><path d="M7.5 8.5L11 15.5" /><path d="M16.5 8.5L13 15.5" /></svg>
              <!-- custom personas: generic code glyph -->
              <svg
                v-else
                class="rit-persona-icon"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              ><polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" /></svg>
              {{ localizedPersonaName(p) }}
              <!-- 菜单项行尾红点（V1 index.html:2005）：该 persona 被自定义过。 -->
              <span
                v-if="p.is_customized"
                class="rit-code-persona-dot rit-code-persona-dot--inline"
                :title="t('codePersona.customizedHint')"
              >●</span>
            </div>
            <div
              v-if="p.description"
              class="rit-submenu-item-desc"
              :title="localizedPersonaDesc(p)"
            >
              {{ localizedPersonaDesc(p) }}
            </div>
          </div>
          <span
            v-if="props.persona === p.id"
            class="rit-submenu-check"
          >✓</span>
        </div>
        <!-- V1 parity (index.html:2011-2020): separator + "Edit prompts..."
             item that deep-links to Settings → Coding Modes for editing
             persona system prompts. Capability itself was never lost
             (CodePersonasPanel still edits them); this restores the
             chat-side one-click entry. -->
        <div
          class="rit-submenu-divider"
          role="separator"
        ></div>
        <div
          class="rit-submenu-item rit-code-persona-edit"
          data-testid="code-persona-edit"
          role="menuitem"
          @click="goEditPersonas"
        >
          <div class="rit-submenu-item-body">
            <div class="rit-submenu-item-label">
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              ><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" /></svg>
              <span>{{ t("codePersona.editPrompts") }}</span>
            </div>
          </div>
        </div>
      </div>
      <div
        v-if="personaMenuOpen"
        class="dropdown-overlay"
        @click="personaMenuOpen = false"
      ></div>
    </div>

    <!-- ── 快速/思考/专家 思考强度下拉（V1 index.html:2025-2070） ── -->
    <div class="rit-submenu-wrap">
      <button
        type="button"
        class="rit-btn"
        data-testid="code-speed-trigger"
        @click="toggleSubmenu"
      >
        <!-- 闪电图标（V1 index.html:2028） -->
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></svg>
        <span>{{ currentLabel() }}</span>
        <svg
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <polyline
            v-if="submenuOpen"
            points="18 15 12 9 6 15"
          />
          <polyline
            v-else
            points="6 9 12 15 18 9"
          />
        </svg>
      </button>
      <div
        v-if="submenuOpen"
        class="rit-submenu"
        role="menu"
      >
        <template
          v-for="s in speeds"
          :key="s.id"
        >
          <div
            class="rit-submenu-item"
            :class="{ active: props.speed === s.id }"
            :data-testid="`code-speed-${s.id}`"
            role="menuitem"
            @click="pickSpeed(s.id)"
          >
            <div class="rit-submenu-item-body">
              <div class="rit-submenu-item-label">
                <!-- fast=闪电 / think=时钟 / expert=星形（V1 index.html:2039/2049/2061） -->
                <svg
                  v-if="s.id === 'fast'"
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                ><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></svg>
                <svg
                  v-else-if="s.id === 'think'"
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                ><circle
                  cx="12"
                  cy="12"
                  r="10"
                /><path d="M12 8v4l3 3" /></svg>
                <svg
                  v-else
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                ><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" /></svg>
                {{ t(s.labelKey) }}
              </div>
              <div class="rit-submenu-item-desc">
                {{ t(s.descKey) }}
              </div>
            </div>
            <span
              v-if="props.speed === s.id"
              class="rit-submenu-check"
            >✓</span>
          </div>
          <!-- V1 index.html:2056 — 分隔线（think 与 expert 之间）。 -->
          <div
            v-if="s.id === 'think'"
            class="rit-submenu-divider"
            role="separator"
          ></div>
        </template>
      </div>
      <div
        v-if="submenuOpen"
        class="dropdown-overlay"
        @click="submenuOpen = false"
      ></div>
    </div>

    <!-- ── 上传代码文件（V1 index.html:2072-2100） ── -->
    <label
      class="rit-btn rit-model-upload"
      :class="{
        'rit-model-upload--active': props.filePath,
        'rit-model-upload--uploading': isUploading,
      }"
      data-testid="code-upload-label"
      :title="
        props.filePath
          ? t('index.uploadedPathTitle', { name: codeFileName, path: props.filePath })
          : t('index.uploadCodeHint')
      "
    >
      <!-- 上传中：旋转动画；否则：回形针图标 -->
      <svg
        v-if="isUploading"
        class="rit-spin"
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <path d="M21 12a9 9 0 1 1-6.219-8.56" />
      </svg>
      <svg
        v-else
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      ><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" /></svg>
      <span v-if="isUploading">{{ t("index.uploadingDots") }}</span>
      <span
        v-else-if="codeFileName"
        class="rit-model-filename"
        :title="codeFileName"
      >{{ codeFileName }}</span>
      <span v-else>{{ t("index.uploadFile") }}</span>
      <!-- Upload success check -->
      <span
        v-if="props.filePath && !isUploading"
        class="rit-upload-ok"
        :title="t('index.uploadedToServer')"
      >✓</span>
      <input
        ref="codeFileInput"
        type="file"
        :accept="CODE_ACCEPT"
        style="display: none"
        data-testid="code-upload-input"
        :disabled="isUploading"
        @change="handleCodeFileSelect"
      />
    </label>

    <!-- ── 引入开源仓库（V1 index.html:2102-2133） ── -->
    <div class="rit-submenu-wrap">
      <button
        type="button"
        class="rit-btn"
        :class="{ 'rit-model-upload--active': props.repoUrl }"
        data-testid="code-repo-trigger"
        :title="
          props.repoUrl
            ? t('index.repoImportedTitle', { url: props.repoUrl })
            : t('index.importRepoFullHint')
        "
        @click="toggleRepoInput"
      >
        <!-- 地球连接图标（V1 index.html:2110） -->
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><circle
          cx="12"
          cy="12"
          r="4"
        /><line
          x1="1.05"
          y1="12"
          x2="7"
          y2="12"
        /><line
          x1="17.01"
          y1="12"
          x2="22.96"
          y2="12"
        /></svg>
        <span
          v-if="props.repoUrl"
          class="rit-model-filename"
          :title="props.repoUrl"
        >{{ repoShortLabel() }}</span>
        <span v-else>{{ t("index.importRepo") }}</span>
        <span
          v-if="props.repoUrl"
          class="rit-upload-ok"
          :title="t('index.repoImportedShortTitle')"
        >✓</span>
      </button>
      <!-- Repo URL input panel -->
      <div
        v-if="repoInputOpen"
        class="rit-submenu"
        data-testid="code-repo-panel"
        style="min-width: 320px; padding: 10px 12px"
      >
        <div
          class="rit-submenu-header"
          style="margin-bottom: 8px"
        >
          {{ t("index.importRepo") }}
        </div>
        <div style="display: flex; gap: 6px; align-items: center">
          <input
            v-model="repoUrlDraft"
            type="url"
            placeholder="https://github.com/owner/repo"
            data-testid="code-repo-input"
            style="
              flex: 1;
              background: var(--bg-input);
              border: 1px solid var(--border, #2d3748);
              border-radius: 5px;
              padding: 5px 8px;
              font-size: var(--text-sm);
              color: var(--text-primary, #e0e6f0);
              outline: none;
            "
            @keydown.enter.prevent="confirmRepoUrl"
            @keydown.escape="repoInputOpen = false"
          />
          <button
            type="button"
            class="btn btn-primary"
            data-testid="code-repo-confirm"
            style="padding: 4px 10px; font-size: var(--text-sm)"
            @click="confirmRepoUrl"
          >
            {{ t("index.confirmShort") }}
          </button>
          <button
            v-if="props.repoUrl"
            type="button"
            class="btn btn-ghost"
            data-testid="code-repo-clear"
            style="padding: 4px 8px; font-size: var(--text-sm)"
            :title="t('index.clearRepo')"
            @click="clearRepo"
          >
            ✕
          </button>
        </div>
        <div
          style="
            font-size: var(--text-xs);
            color: var(--text-muted, #6b7a99);
            margin-top: 6px;
          "
        >
          {{ t("index.supportRepoHint") }}
        </div>
      </div>
      <div
        v-if="repoInputOpen"
        class="dropdown-overlay"
        @click="repoInputOpen = false"
      ></div>
    </div>
  </div>
</template>
