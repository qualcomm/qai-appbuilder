<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChannelsGuideDialog — the generic channels usage guide (V1 "channel" help
 * modal triggered by the topbar "Usage Guide" button). Shows the smart
 * model-switching section (shared by WeChat + Feishu) and the conversation
 * command reference.
 *
 * Smart-switch bodies carry inline markup from our own static i18n catalog
 * (trusted), so `v-html` is safe here.
 */
import { useI18n } from "vue-i18n";

import ChannelInfoDialog from "./ChannelInfoDialog.vue";
import ChannelCommandTable from "./ChannelCommandTable.vue";
import ChannelCcOcCommandTable from "./ChannelCcOcCommandTable.vue";

defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: [] }>();

const { t } = useI18n();

const smartKeys = [
  "channels.guide.smart1",
  "channels.guide.smart2",
  "channels.guide.smart3",
  "channels.guide.smart4",
];
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- v-html renders only our own static, trusted i18n catalog strings (no user/remote input); not an XSS vector. -->
  <ChannelInfoDialog
    :open="open"
    icon="📖"
    :title="t('channels.guide.title')"
    :subtitle="t('channels.guide.subtitle')"
    @close="emit('close')"
  >
    <section>
      <div class="channel-info-section-title">
        {{ t("channels.guide.smartTitle") }}
      </div>
      <ul class="channel-info-notice">
        <li
          v-for="k in smartKeys"
          :key="k"
          v-html="t(k)"
        />
      </ul>
    </section>

    <section>
      <div class="channel-info-section-title">
        {{ t("channels.cmd.sectionTitle") }}
      </div>
      <ChannelCommandTable
        variant="full"
        reboot-desc-key="channels.cmd.rebootFull"
      />
    </section>

    <!-- /cc + /oc command catalogues (V1 app.js:252-315 parity). -->
    <section>
      <ChannelCcOcCommandTable />
    </section>
  </ChannelInfoDialog>
  <!-- eslint-enable vue/no-v-html -->
</template>
