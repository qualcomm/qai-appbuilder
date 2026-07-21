<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChannelCcOcCommandTable — V1-parity reference for the `/cc` (Claude Code)
 * and `/oc` (Open Code) channel command catalogues.
 *
 * V1 rendered both sections inside the WeChat / Feishu info dialogs and the
 * generic Channels usage guide (legacy `app.js:252-315`); V2 had only the
 * generic `ChannelCommandTable` (普通对话命令), losing the entire `/cc` +
 * `/oc` documentation. This component fills that gap.
 *
 * Each section follows the V1 layout:
 *   • emoji + title
 *   • 3-bullet "tips" list (HTML allowed via i18n — the strings are owned
 *     and trusted, identical safety model to the parent dialogs)
 *   • command rows: `code label` + description (from i18n key)
 *   • `aliasNote` footnote (HTML allowed)
 *
 * i18n namespace: `app.botGuide.cc.*` and `app.botGuide.oc.*` (already
 * present in en / zh-CN / zh-TW; no locale changes required).
 *
 * Order of command rows mirrors V1 `app.js:261-274` (cc) and `:300-311` (oc)
 * 1:1. Some labels are hard-coded literals in V1 (no i18n key); they stay
 * literals here too — they are operator commands, not user-facing copy that
 * needs translation. Labels with parameters / emphasis use V1's *Label keys.
 */
import { useI18n } from "vue-i18n";

const { t } = useI18n();

/** A command row: [literal label OR i18n labelKey, descKey, isLabelKey?]. */
type Row = readonly [label: string, descKey: string, isLabelKey?: boolean];

/** V1 `app.js:261-274` — Claude Code command catalogue (in V1 order). */
const ccRows: ReadonlyArray<Row> = [
  ["app.botGuide.cc.newLabel", "app.botGuide.cc.newDesc", true],
  ["/cc list <em>(/cc l)</em>", "app.botGuide.cc.listDesc"],
  ["app.botGuide.cc.useNumLabel", "app.botGuide.cc.useNumDesc", true],
  ["app.botGuide.cc.useIdLabel", "app.botGuide.cc.useIdDesc", true],
  ["/cc status <em>(/cc s)</em>", "app.botGuide.cc.statusDesc"],
  ["/cc models <em>(/cc ms)</em>", "app.botGuide.cc.modelsDesc"],
  ["/cc model [N] <em>(/cc m)</em>", "app.botGuide.cc.modelDesc"],
  ["/cc fork <em>(/cc f)</em>", "app.botGuide.cc.forkDesc"],
  ["/cc stop <em>(/cc st)</em>", "app.botGuide.cc.stopDesc"],
  ["/cc cd [path]", "app.botGuide.cc.cdDesc"],
  ["app.botGuide.cc.renameLabel", "app.botGuide.cc.renameDesc", true],
  ["/cc close <em>(/cc c)</em>", "app.botGuide.cc.closeDesc"],
  ["/cc delete <em>(/cc d)</em>", "app.botGuide.cc.deleteDesc"],
  ["/cc help <em>(/cc h)</em>", "app.botGuide.cc.helpDesc"],
];

/** V1 `app.js:300-311` — Open Code command catalogue (in V1 order). */
const ocRows: ReadonlyArray<Row> = [
  ["app.botGuide.oc.newLabel", "app.botGuide.oc.newDesc", true],
  ["/oc list <em>(/oc l)</em>", "app.botGuide.oc.listDesc"],
  ["app.botGuide.oc.useNumLabel", "app.botGuide.oc.useNumDesc", true],
  ["app.botGuide.oc.useIdLabel", "app.botGuide.oc.useIdDesc", true],
  ["/oc status <em>(/oc s)</em>", "app.botGuide.oc.statusDesc"],
  ["/oc models <em>(/oc ms)</em>", "app.botGuide.oc.modelsDesc"],
  ["/oc model [N] <em>(/oc m)</em>", "app.botGuide.oc.modelDesc"],
  ["/oc stop <em>(/oc st)</em>", "app.botGuide.oc.stopDesc"],
  ["app.botGuide.oc.renameLabel", "app.botGuide.oc.renameDesc", true],
  ["/oc close <em>(/oc c)</em>", "app.botGuide.oc.closeDesc"],
  ["/oc delete <em>(/oc d)</em>", "app.botGuide.oc.deleteDesc"],
  ["/oc help <em>(/oc h)</em>", "app.botGuide.oc.helpDesc"],
];

const ccTipKeys = ["app.botGuide.cc.tip1", "app.botGuide.cc.tip2", "app.botGuide.cc.tip3"] as const;
const ocTipKeys = ["app.botGuide.oc.tip1", "app.botGuide.oc.tip2", "app.botGuide.oc.tip3"] as const;
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- v-html renders only our own static, trusted i18n catalog strings (no user/remote input); not an XSS vector. -->
  <div class="cc-oc-cmds">
    <!-- /cc section ────────────────────────────────────────────────── -->
    <section class="cc-oc-section">
      <div class="cc-oc-section__title">
        {{ t("app.botGuide.cc.title") }}
      </div>
      <ul class="cc-oc-tips">
        <li
          v-for="k in ccTipKeys"
          :key="k"
          v-html="t(k)"
        />
      </ul>
      <div class="channel-cmds">
        <div
          v-for="[label, descKey, isLabelKey] in ccRows"
          :key="label + descKey"
          class="channel-cmd-row"
        >
          <span
            class="channel-cmd-code"
            v-html="isLabelKey ? t(label) : label"
          />
          <div
            class="channel-cmd-desc"
            v-html="t(descKey)"
          />
        </div>
      </div>
      <p
        class="cc-oc-aliases"
        v-html="t('app.botGuide.cc.aliasNote')"
      />
    </section>

    <!-- /oc section ────────────────────────────────────────────────── -->
    <section class="cc-oc-section">
      <div class="cc-oc-section__title">
        {{ t("app.botGuide.oc.title") }}
      </div>
      <ul class="cc-oc-tips">
        <li
          v-for="k in ocTipKeys"
          :key="k"
          v-html="t(k)"
        />
      </ul>
      <div class="channel-cmds">
        <div
          v-for="[label, descKey, isLabelKey] in ocRows"
          :key="label + descKey"
          class="channel-cmd-row"
        >
          <span
            class="channel-cmd-code"
            v-html="isLabelKey ? t(label) : label"
          />
          <div
            class="channel-cmd-desc"
            v-html="t(descKey)"
          />
        </div>
      </div>
      <p
        class="cc-oc-aliases"
        v-html="t('app.botGuide.oc.aliasNote')"
      />
    </section>
  </div>
  <!-- eslint-enable vue/no-v-html -->
</template>

<style scoped>
.cc-oc-cmds {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.cc-oc-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.cc-oc-section__title {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--text-primary);
}

.cc-oc-tips {
  margin: 0;
  padding-left: var(--space-4);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  line-height: 1.6;
}

.cc-oc-tips :deep(strong) {
  color: var(--text-primary);
}

/* Reuse the visual tokens already defined by ChannelCommandTable's
   `.channel-cmds / .channel-cmd-row / .channel-cmd-code / .channel-cmd-desc`
   classes so this table looks identical to the conversation-commands one
   (V1 used a single shared block for both sections). */
.channel-cmds {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.channel-cmd-row {
  display: flex;
  gap: 10px;
  align-items: baseline;
}

.channel-cmd-code {
  flex-shrink: 0;
  min-width: 180px;
  font-family: var(--font-mono, monospace);
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-primary);
}

.channel-cmd-code :deep(em) {
  font-weight: 400;
  opacity: 0.7;
}

.channel-cmd-desc {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  line-height: 1.5;
}

.channel-cmd-desc :deep(code) {
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--bg-secondary);
  font-family: var(--font-mono, monospace);
  font-size: 0.95em;
}

.channel-cmd-desc :deep(em) {
  font-style: italic;
  color: var(--text-muted);
}

.cc-oc-aliases {
  margin: 0;
  padding: var(--space-2) 0 0;
  font-size: var(--text-xs);
  color: var(--text-muted);
  line-height: 1.6;
}

.cc-oc-aliases :deep(code) {
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--bg-secondary);
  font-family: var(--font-mono, monospace);
}
</style>
