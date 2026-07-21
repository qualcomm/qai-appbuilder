<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChannelCommandTable — the shared "conversation commands" reference rendered
 * inside the WeChat / Feishu info dialogs and the generic usage guide
 * (V1 `index.html` `wechat-help-cmds` block).
 *
 * V1 sourced the command descriptions from the `index.cmd.*` namespace; in V2
 * the channel-scoped copy lives under `channels.cmd.*` (this file's owned
 * namespace) so the Channels view is self-contained and does not depend on
 * the chat-view command catalog.
 *
 * V1 renders two flavours of this table (`index.html`):
 *  - **short** — terse one-liners used in the WeChat / Feishu ℹ️ info dialogs
 *    (`index.cmd.*DescShort`, lines 2504-2521).
 *  - **full** — verbose descriptions (with `<strong>`/`<br>` markup + a
 *    "Reply: …" example) used in the generic usage guide (`index.cmd.*DescFull`,
 *    lines 2419-2437). Full rows also use Chinese-friendly argument
 *    placeholders (`/use <编号>`, `/rename <名称>`, `/model [编号]`,
 *    `/compact [轮次]`) matching V1.
 *
 * The caller selects the flavour via `variant` (default `"short"`). Full
 * descriptions carry trusted inline markup from our own static i18n catalog,
 * so `v-html` is safe for that flavour.
 *
 * `reboot` differs per channel (WeChat / Feishu / generic), so the caller
 * passes the desired reboot description key.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";

const props = withDefaults(
  defineProps<{
    /** i18n key for the channel-specific `/reboot` description. */
    rebootDescKey: string;
    /** `"short"` (info dialogs) or `"full"` (usage guide). */
    variant?: "short" | "full";
  }>(),
  { variant: "short" },
);

const { t } = useI18n();

/**
 * [shortLabel, fullLabel, alias, shortKey, fullKey] — V1 command order.
 *
 * `shortLabel`/`fullLabel` differ only for parameterised commands, where the
 * full usage guide uses Chinese-friendly placeholders matching V1.
 */
const rows: ReadonlyArray<
  readonly [string, string, string, string, string]
> = [
  ["/help", "/help", "/h", "channels.cmd.help", "channels.cmd.helpFull"],
  ["/new", "/new", "/n", "channels.cmd.new", "channels.cmd.newFull"],
  ["/clear", "/clear", "/cl", "channels.cmd.clear", "channels.cmd.clearFull"],
  ["/list [N]", "/list [N]", "/l", "channels.cmd.list", "channels.cmd.listFull"],
  ["/use <#>", "/use <编号>", "/u", "channels.cmd.use", "channels.cmd.useFull"],
  ["/status", "/status", "/s", "channels.cmd.status", "channels.cmd.statusFull"],
  [
    "/rename <name>",
    "/rename <名称>",
    "/rn",
    "channels.cmd.rename",
    "channels.cmd.renameFull",
  ],
  ["/delete", "/delete", "/del", "channels.cmd.delete", "channels.cmd.deleteFull"],
  ["/stop", "/stop", "/st", "channels.cmd.stop", "channels.cmd.stopFull"],
  ["/models", "/models", "/ms", "channels.cmd.models", "channels.cmd.modelsFull"],
  ["/model [#]", "/model [编号]", "/m", "channels.cmd.model", "channels.cmd.modelFull"],
  [
    "/compact [N]",
    "/compact [轮次]",
    "/c",
    "channels.cmd.compact",
    "channels.cmd.compactFull",
  ],
];

const isFull = computed(() => props.variant === "full");
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- v-html renders only our own static, trusted i18n catalog strings (no user/remote input); not an XSS vector. -->
  <div class="channel-cmds">
    <div
      v-for="[shortLabel, fullLabel, alias, shortKey, fullKey] in rows"
      :key="shortKey"
      class="channel-cmd-row"
    >
      <span class="channel-cmd-code">{{ isFull ? fullLabel : shortLabel }} <em>({{ alias }})</em></span>
      <div
        v-if="isFull"
        class="channel-cmd-desc"
        v-html="t(fullKey)"
      />
      <div
        v-else
        class="channel-cmd-desc"
      >
        {{ t(shortKey) }}
      </div>
    </div>
    <div class="channel-cmd-row">
      <span class="channel-cmd-code">/reboot <em>(/r)</em></span>
      <div
        v-if="isFull"
        class="channel-cmd-desc"
        v-html="t(rebootDescKey)"
      />
      <div
        v-else
        class="channel-cmd-desc"
      >
        {{ t(rebootDescKey) }}
      </div>
    </div>
  </div>
  <!-- eslint-enable vue/no-v-html -->
</template>

<style scoped>
.channel-cmds {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.channel-cmd-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--space-3) var(--space-4);
}

.channel-cmd-code {
  flex-shrink: 0;
  min-width: 120px;
  font-family: var(--font-mono, monospace);
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-light);
  border-radius: var(--radius-xs);
  padding: 2px 7px;
}

.channel-cmd-code em {
  font-weight: 400;
  opacity: 0.7;
}

.channel-cmd-desc {
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
</style>
