<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChannelModelSelector — per-channel AI model picker (V1 parity).
 *
 * V1 layout (`channels/wechat/WechatConfigPanel.js:82-133` +
 * `css/channels.css:477-504`): a single horizontal `.channel-model-row`
 * (label on the left, a transparent borderless `.channel-model-select`
 * trigger on the right, separated from the body above by a `border-top`).
 * Clicking the trigger opens an absolutely-positioned dropdown with a
 * search box + provider-grouped candidate list (reusing the global
 * `.model-item` / `.model-group-label` / `.divider` classes), a
 * "follow global" default option, and a ✓ on the current selection.
 *
 * Selecting an entry POSTs to `/api/{kind}/model`; the current value is
 * loaded via GET on mount.
 *
 * Owns its own `useChannelSettings(kind, instanceId)` instance so it
 * can be reused per-channel without prop drilling.
 */
import { onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";

import { useToastStore } from "@/stores/toast";
import { useChannelSettings, type ChannelKind } from "@/composables/useChannelSettings";
import type { CloudModelEntry } from "@/types/cloudModels";

const props = defineProps<{
  kind: ChannelKind;
  instanceId: string;
  /**
   * Single-instance resolver: registers / returns the channel ULID on demand
   * so the user can pick a model BEFORE connecting (v0.5 parity). When the
   * panel has no instance yet (`instanceId === ""`), saving a model resolves
   * one transparently instead of failing.
   */
  resolveInstanceId?: () => Promise<string | null>;
}>();

const { t } = useI18n();
const toast = useToastStore();

const {
  modelId,
  modelSearch,
  modelGroups,
  selectedModelLabel,
  isSelected,
  saving,
  loadModelCandidates,
  loadModel,
  saveModel,
} = useChannelSettings(props.kind, props.instanceId, props.resolveInstanceId);

const open = ref(false);

function toggle(): void {
  open.value = !open.value;
  if (open.value) modelSearch.value = "";
}

async function pick(entry: CloudModelEntry | null): Promise<void> {
  const ok = await saveModel(entry);
  open.value = false;
  toast.push({
    id: crypto.randomUUID(),
    kind: ok ? "success" : "error",
    message: ok
      ? t("channels.modelSaved", "Channel model saved")
      : t("channels.modelSaveFailed", "Failed to save channel model"),
    timeoutMs: ok ? 2500 : 5000,
  });
}

onMounted(async () => {
  await Promise.all([
    loadModelCandidates(t("chat.modelDropdownGroupLocal", "Local Models")),
    loadModel(),
  ]);
});
</script>

<template>
  <!-- V1 parity: horizontal `.channel-model-row` (label left + transparent
       trigger right + top separator). -->
  <div
    class="channel-model-row"
    :data-testid="`${kind}-model-selector`"
  >
    <span class="channel-model-label">{{ t("channels.modelLabel", "AI Model") }}</span>
    <div class="channel-model-field">
      <button
        type="button"
        class="channel-model-select"
        :disabled="saving"
        :data-testid="`${kind}-model-trigger`"
        @click="toggle"
      >
        <span class="channel-model-select__text">
          {{ modelId ? selectedModelLabel : t("channels.modelFollowGlobal", "Follow global setting") }}
        </span>
        <span class="channel-model-select__caret">▾</span>
      </button>

      <div
        v-if="open"
        class="channel-model-dropdown"
        :data-testid="`${kind}-model-dropdown`"
      >
        <div class="channel-model-dropdown__search">
          <input
            v-model="modelSearch"
            class="config-input"
            type="text"
            :placeholder="t('channels.modelSearch', 'Search models...')"
            :data-testid="`${kind}-model-search`"
            @click.stop
          />
        </div>

        <div class="channel-model-dropdown__list">
          <!-- Follow-global default option -->
          <div
            class="model-item"
            :class="{ selected: !modelId }"
            @click="pick(null)"
          >
            <span class="model-item-default">{{ t("channels.modelFollowGlobal", "Follow global setting") }}</span>
            <span
              v-if="!modelId"
              class="model-item-check"
            >✓</span>
          </div>

          <template
            v-for="group in modelGroups"
            :key="group.provider"
          >
            <div
              v-if="group.isSeparator"
              class="divider"
              aria-hidden="true"
            />
            <template v-else>
              <div class="model-group-label">
                <span v-if="group.pinned">📌 </span>{{ group.provider }}
              </div>
              <div
                v-for="m in group.models"
                :key="`${group.provider}::${m.model_id}`"
                class="model-item"
                :class="{ selected: isSelected(m) }"
                @click="pick(m)"
              >
                <span class="model-item-name">{{ m.name }}</span>
                <span
                  v-if="isSelected(m)"
                  class="model-item-check"
                >✓</span>
              </div>
            </template>
          </template>

          <div
            v-if="modelGroups.length === 0"
            class="model-item placeholder"
          >
            {{ t("channels.modelNoCandidates", "No models available") }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* V1 parity: the row + label + transparent select reuse the global
   `.channel-model-row` / `.channel-model-label` / `.channel-model-select`
   classes (styles channels.css). Only the dropdown overlay (V1 used inline
   styles) is defined here, scoped, with real CSS-variable names. */
.channel-model-field {
  flex: 1;
  min-width: 0;
  position: relative;
}

.channel-model-select {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  gap: 4px;
  text-align: left;
}

.channel-model-select__text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.channel-model-select__caret {
  font-size: var(--text-xs);
  opacity: 0.6;
  flex-shrink: 0;
  margin-left: 4px;
}

.channel-model-dropdown {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  z-index: 100;
  margin-top: 4px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-secondary);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}

.channel-model-dropdown__search {
  padding: 8px;
}

.channel-model-dropdown__search .config-input {
  width: 100%;
  font-size: var(--text-sm);
}

.channel-model-dropdown__list {
  max-height: 240px;
  overflow-y: auto;
}

.model-item-default {
  opacity: 0.6;
}

.model-item-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
