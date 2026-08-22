<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModeFrameTranslate — chat-input sub-toolbar for `translate` mode.
 *
 * P4-A T2.7-A: exit badge + target-language picker (`zh-CN` / `en` /
 * `zh-TW`) + an auto-detect toggle. Mirrors V1 behaviour: when
 * auto-detect is on, the language is picked from input content
 * heuristically; the manual select forces a fixed target language.
 */
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import { useToast } from "@/composables/useToast";

const { t } = useI18n();
const toast = useToast();

type TranslateLang = "zh-CN" | "en" | "zh-TW";

const props = withDefaults(
  defineProps<{
    lang?: TranslateLang;
    autoDetect?: boolean;
  }>(),
  {
    lang: "zh-CN",
    autoDetect: true,
  },
);

const emit = defineEmits<{
  exit: [];
  "update:lang": [value: TranslateLang];
  "update:autoDetect": [value: boolean];
}>();

const submenuOpen = ref(false);

// V1 index.html:2181-2204 顺序：English / 简体中文 / 繁体中文。
const langs: ReadonlyArray<{ id: TranslateLang; labelKey: string }> = [
  { id: "en", labelKey: "language.en" },
  { id: "zh-CN", labelKey: "index.langZhCN" },
  { id: "zh-TW", labelKey: "index.langZhTW" },
];

function onExit(): void {
  emit("exit");
}

function pickLang(id: TranslateLang): void {
  emit("update:lang", id);
  // 手动选择后固定语言（V1 translateLangManual = true → autoDetect = false）。
  if (props.autoDetect) emit("update:autoDetect", false);
  submenuOpen.value = false;
}

// 重置为自动检测（V1 index.html:2174-2179）。
function resetToAuto(): void {
  emit("update:autoDetect", true);
  submenuOpen.value = false;
}

// 按钮上显示的目标语言名（V1 index.html:2162）。
function currentLangName(): string {
  if (props.lang === "en") return "English";
  if (props.lang === "zh-CN") return t("index.langZhCN");
  return t("index.langZhTW");
}

// 上传文件：V1 未实现，点击 toast 提示（index.html:2151-2155）。
function onUploadNotImpl(): void {
  toast.warning(t("index.translateUploadNotImplToast"));
}
</script>

<template>
  <div
    class="rit-left"
    data-testid="mode-frame-translate"
  >
    <button
      type="button"
      class="rit-mode-badge"
      data-testid="mode-frame-exit"
      :title="t('toolbar.exitMode', { mode: t('toolbar.translate') })"
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
        <path d="M5 8l6 6" />
        <path d="M4 14l6-6 2-3" />
        <path d="M2 5h12" />
        <path d="M7 2h1" />
        <path d="M22 22l-5-10-5 10" />
        <path d="M14 18h6" />
      </svg>
      <span>{{ t("index.translateMode") }}</span>
      <span class="rit-close">✕</span>
    </button>

    <span class="rit-sep"></span>

    <!-- 上传文件：暂未实现，置灰不可用（V1 index.html:2150-2155） -->
    <label
      class="rit-btn rit-btn--disabled"
      data-testid="translate-upload-disabled"
      :title="t('index.uploadFileNotImpl')"
      @click.prevent="onUploadNotImpl"
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
      ><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" /></svg>
      <span>{{ t("index.uploadFile") }}</span>
    </label>

    <!-- 目标语言下拉（V1 index.html:2159-2206） -->
    <div class="rit-submenu-wrap">
      <button
        type="button"
        class="rit-btn"
        data-testid="translate-lang-trigger"
        @click="submenuOpen = !submenuOpen"
      >
        <!-- 地球图标（V1 index.html:2161） -->
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
          r="10"
        /><line
          x1="2"
          y1="12"
          x2="22"
          y2="12"
        /><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" /></svg>
        <span>{{ t("index.translateToPrefix", { lang: currentLangName() }) }}</span>
        <!-- 自动检测标记：未手动选择时显示（V1 index.html:2164） -->
        <span
          v-if="props.autoDetect"
          class="rit-translate-auto"
          :title="t('index.autoDetectLangHint')"
        >{{ t("index.autoLabel") }}</span>
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
        <div class="rit-submenu-header">
          {{ t("index.translateHeader") }}
          <!-- 重置为自动检测（V1 index.html:2174-2179） -->
          <span
            v-if="!props.autoDetect"
            class="rit-translate-reset"
            data-testid="translate-reset-auto"
            :title="t('index.resetToAutoDetect')"
            @click.stop="resetToAuto"
          >
            {{ t("index.autoResetButton") }}
          </span>
        </div>
        <div
          v-for="l in langs"
          :key="l.id"
          class="rit-submenu-item"
          :class="{ active: props.lang === l.id }"
          :data-testid="`translate-lang-${l.id}`"
          role="menuitem"
          @click="pickLang(l.id)"
        >
          <div class="rit-submenu-item-body">
            <div class="rit-submenu-item-label">
              {{ l.id === "en" ? "English" : t(l.labelKey) }}
            </div>
          </div>
          <span
            v-if="props.lang === l.id"
            class="rit-submenu-check"
          >✓</span>
        </div>
      </div>
      <div
        v-if="submenuOpen"
        class="dropdown-overlay"
        @click="submenuOpen = false"
      ></div>
    </div>
  </div>
</template>
