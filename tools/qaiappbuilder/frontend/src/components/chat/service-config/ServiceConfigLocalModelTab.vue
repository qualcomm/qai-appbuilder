<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Local Model Tab — service_config ``local_model`` master switch only.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { ServiceConfig } from "./types";
import { localModel } from "./helpers";
import CollapsibleCard from "./CollapsibleCard.vue";
import ToggleSwitch from "./ToggleSwitch.vue";

const props = defineProps<{
  cfg: ServiceConfig;
  saving: boolean;
  hideSave?: boolean;
}>();

const emit = defineEmits<{ (e: "save"): void }>();

const { t } = useI18n();

// Alias the (reactive) config prop so templates mutate nested properties of the
// parent-owned object via a local ref rather than tripping vue/no-mutating-props
// on the prop path; the computed always reflects the current prop value even
// when the parent reassigns ``svcCfg``.
const cfg = computed(() => props.cfg);
</script>

<template>
  <div class="service-config-panel__form">
    <!-- 🖥️ Local Model master switch -->
    <CollapsibleCard>
      <template #title>
        🖥️ {{ t("serviceConfig.localModelTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.enableLocalModel") }}</span>
        <ToggleSwitch
          v-model="localModel(cfg).enabled"
          :aria-label="t('serviceConfig.enableLocalModel')"
        />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.localModelHint") }}</span>
    </CollapsibleCard>

    <button
      v-if="!props.hideSave"
      type="button"
      class="service-config-panel__save"
      :disabled="props.saving"
      @click="emit('save')"
    >
      {{ props.saving ? t("common.saving") : t("common.save") }}
    </button>
  </div>
</template>

<style scoped src="./service-config.css"></style>
