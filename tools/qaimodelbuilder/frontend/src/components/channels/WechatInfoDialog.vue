<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * WechatInfoDialog — the WeChat-specific info dialog (V1 `wechat-help-modal`
 * triggered by the ℹ️ button on the WeChat card). Shows the "notes" section
 * (iLink Bot / silent reconnect / per-user history / image support) and the
 * conversation command reference.
 *
 * Note bodies carry inline `<strong>` markup and come from our own static
 * i18n catalog (trusted), so `v-html` is safe here (no user input).
 */
import { useI18n } from "vue-i18n";

import ChannelInfoDialog from "./ChannelInfoDialog.vue";
import ChannelCommandTable from "./ChannelCommandTable.vue";
import ChannelCcOcCommandTable from "./ChannelCcOcCommandTable.vue";

defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: [] }>();

const { t } = useI18n();

const noteKeys = [
  "channels.wechat.info.note1",
  "channels.wechat.info.note2",
  "channels.wechat.info.note3",
  "channels.wechat.info.note4",
  "channels.wechat.info.note5",
];
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- v-html renders only our own static, trusted i18n catalog strings (no user/remote input); not an XSS vector. -->
  <ChannelInfoDialog
    :open="open"
    icon="🟢"
    :title="t('channels.wechat.info.title')"
    :subtitle="t('channels.wechat.info.subtitle')"
    @close="emit('close')"
  >
    <!-- V1 parity (index.html:2276-2279): WeChat brand SVG (two #07C160
         ellipses) in the header icon box, not a bare emoji. -->
    <template #icon>
      <svg
        width="22"
        height="22"
        viewBox="0 0 24 24"
        fill="none"
        style="vertical-align: middle"
      >
        <ellipse
          cx="9"
          cy="9"
          rx="7"
          ry="5.5"
          fill="#07C160"
        />
        <ellipse
          cx="16"
          cy="15"
          rx="6"
          ry="4.5"
          fill="#07C160"
          opacity="0.65"
        />
      </svg>
    </template>

    <section>
      <div class="channel-info-section-title">
        {{ t("channels.wechat.info.notesTitle") }}
      </div>
      <ul class="channel-info-notice">
        <li
          v-for="k in noteKeys"
          :key="k"
          v-html="t(k)"
        />
      </ul>
    </section>

    <section>
      <div class="channel-info-section-title">
        {{ t("channels.cmd.sectionTitle") }}
      </div>
      <ChannelCommandTable reboot-desc-key="channels.cmd.rebootWechat" />
    </section>

    <!-- /cc + /oc command catalogues (V1 index.html:2270-2326 parity). -->
    <section>
      <ChannelCcOcCommandTable />
    </section>
  </ChannelInfoDialog>
  <!-- eslint-enable vue/no-v-html -->
</template>
