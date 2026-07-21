<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModeFramePpt — chat-input sub-toolbar for `ppt` mode.
 *
 * V1 parity (index.html L1912-1942): exit badge + length picker
 * (`smart` / `short` / `medium` / `long`). The selected length is
 * emitted up to ChatComposer and aggregated into `tool_params.length`,
 * which the backend renders into a Chinese page-count instruction
 * (legacy `_render_ppt_params`).
 */
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import { useToast } from "@/composables/useToast";

const { t } = useI18n();
const toast = useToast();

type PptLength = "smart" | "short" | "medium" | "long";

const props = withDefaults(
  defineProps<{
    length?: PptLength;
  }>(),
  {
    length: "smart",
  },
);

const emit = defineEmits<{
  exit: [];
  "update:length": [value: PptLength];
}>();

const submenuOpen = ref(false);
// V1 index.html:1920-1942 顺序：smart / short / medium / long；
// label key 与 V1 一致（index.lengthSmart/Short/Medium/Long）。
const lengths: ReadonlyArray<{ id: PptLength; labelKey: string }> = [
  { id: "smart", labelKey: "index.lengthSmart" },
  { id: "short", labelKey: "index.lengthShort" },
  { id: "medium", labelKey: "index.lengthMedium" },
  { id: "long", labelKey: "index.lengthLong" },
];

function onExit(): void {
  emit("exit");
}

function pickLength(id: PptLength): void {
  emit("update:length", id);
  submenuOpen.value = false;
}

// 上传文件：V1 未实现，点击 toast 提示（index.html:1903-1907）。
function onUploadNotImpl(): void {
  toast.warning(t("index.pptGenNotImplToast"));
}
</script>

<template>
  <div
    class="rit-left"
    data-testid="mode-frame-ppt"
  >
    <button
      type="button"
      class="rit-mode-badge"
      data-testid="mode-frame-exit"
      :title="t('toolbar.exitMode', { mode: t('toolbar.ppt') })"
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
        <rect
          x="2"
          y="3"
          width="20"
          height="14"
          rx="2"
        />
        <line
          x1="8"
          y1="21"
          x2="16"
          y2="21"
        />
        <line
          x1="12"
          y1="17"
          x2="12"
          y2="21"
        />
      </svg>
      <span>{{ t("index.pptGen") }}</span>
      <span class="rit-close">✕</span>
    </button>

    <span class="rit-sep"></span>

    <!-- 上传文件：暂未实现，置灰不可用（V1 index.html:1902-1907） -->
    <label
      class="rit-btn rit-btn--disabled"
      data-testid="ppt-upload-disabled"
      :title="t('index.pptUploadNotImpl')"
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

    <!-- Length 下拉（V1 index.html:1909-1946） -->
    <div class="rit-submenu-wrap">
      <button
        type="button"
        class="rit-btn"
        data-testid="ppt-length-trigger"
        @click="submenuOpen = !submenuOpen"
      >
        <!-- 列表图标（V1 index.html:1911） -->
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><line
          x1="21"
          y1="10"
          x2="3"
          y2="10"
        /><line
          x1="21"
          y1="6"
          x2="3"
          y2="6"
        /><line
          x1="21"
          y1="14"
          x2="3"
          y2="14"
        /><line
          x1="21"
          y1="18"
          x2="9"
          y2="18"
        /></svg>
        <span>{{ t("index.pptLength") }}</span>
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
          {{ t("index.pptLength") }}
        </div>
        <div
          v-for="l in lengths"
          :key="l.id"
          class="rit-submenu-item"
          :class="{ active: props.length === l.id }"
          :data-testid="`ppt-length-${l.id}`"
          role="menuitem"
          @click="pickLength(l.id)"
        >
          <div class="rit-submenu-item-body">
            <div class="rit-submenu-item-label">
              {{ t(l.labelKey) }}
            </div>
          </div>
          <span
            v-if="props.length === l.id"
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
