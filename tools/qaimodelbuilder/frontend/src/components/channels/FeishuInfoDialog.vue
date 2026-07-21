<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * FeishuInfoDialog — the Feishu-specific info dialog (V1 `feishu` info modal
 * triggered by the ℹ️ button on the Feishu card). Shows configuration steps,
 * advantages, notes, and the conversation command reference.
 *
 * Step/advantage/note bodies carry inline `<strong>`/`<code>`/`<a>` markup and
 * come from our own static i18n catalog (trusted), so `v-html` is safe here.
 */
import { useI18n } from "vue-i18n";

import ChannelInfoDialog from "./ChannelInfoDialog.vue";
import ChannelCommandTable from "./ChannelCommandTable.vue";
import ChannelCcOcCommandTable from "./ChannelCcOcCommandTable.vue";

defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: [] }>();

const { t } = useI18n();

const stepKeys = [
  "channels.feishu.info.step1",
  "channels.feishu.info.step2",
  "channels.feishu.info.step3",
  "channels.feishu.info.step4",
  "channels.feishu.info.step5",
  "channels.feishu.info.step6",
];
const advKeys = [
  "channels.feishu.info.adv1",
  "channels.feishu.info.adv2",
  "channels.feishu.info.adv3",
  "channels.feishu.info.adv4",
];
const noteKeys = [
  "channels.feishu.info.note1",
  "channels.feishu.info.note2",
  "channels.feishu.info.note3",
];
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- v-html renders only our own static, trusted i18n catalog strings (no user/remote input); not an XSS vector. -->
  <ChannelInfoDialog
    :open="open"
    icon="🪶"
    :title="t('channels.feishu.info.title')"
    :subtitle="t('channels.feishu.info.subtitle')"
    @close="emit('close')"
  >
    <section>
      <div class="channel-info-section-title">
        {{ t("channels.feishu.info.stepsTitle") }}
      </div>
      <ol class="channel-info-notice">
        <li
          v-for="k in stepKeys"
          :key="k"
          v-html="t(k)"
        />
      </ol>
    </section>

    <section>
      <div class="channel-info-section-title">
        {{ t("channels.feishu.info.advantagesTitle") }}
      </div>
      <ul class="channel-info-notice channel-info-notice--adv">
        <li
          v-for="k in advKeys"
          :key="k"
          v-html="t(k)"
        />
      </ul>
    </section>

    <section>
      <div class="channel-info-section-title">
        {{ t("channels.feishu.info.notesTitle") }}
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
      <ChannelCommandTable reboot-desc-key="channels.cmd.rebootFeishu" />
    </section>

    <!-- /cc + /oc command catalogues (V1 index.html:2455-2530 parity). -->
    <section>
      <ChannelCcOcCommandTable />
    </section>
  </ChannelInfoDialog>
  <!-- eslint-enable vue/no-v-html -->
</template>
