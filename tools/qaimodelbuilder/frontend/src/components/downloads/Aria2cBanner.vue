<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  Aria2cBanner — V1 5-state banner above the Download Center tabs.

  V1 reference: DownloadCenterPanel.js:104-152
  States (priority order; mutually exclusive):
    1. installing  — spinner + bin_dir hint                  (info)
    2. failed      — install_error + manual download links   (error)
    3. available   — green check + exe_path + daemon pid     (success)
    4. can_auto_install — info + "auto-install on first DL"  (info)
    5. missing     — single-thread fallback notice + links   (warning)

  V1 external links (kept verbatim):
    https://github.com/aria2/aria2/releases
    https://github.com/minnyres/aria2-windows-arm64/releases
-->
<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";

import type { Aria2cBannerState, Aria2cStatus } from "@/types/downloads";

interface Props {
  status: Aria2cStatus;
  banner: Aria2cBannerState;
}

const props = defineProps<Props>();
const { t } = useI18n();

/** V1 tone class: success / info / error / warning. */
const tone = computed<"success" | "info" | "error" | "warning">(() => {
  switch (props.banner) {
    case "available":
      return props.status.daemon_running ? "success" : "info";
    case "installing":
      return "info";
    case "failed":
      return "error";
    case "can_auto_install":
      return "info";
    case "missing":
      return "warning";
  }
  // Fallback for any unforeseen banner value (keeps the computed total).
  return "info";
});

const ARIA2_RELEASES_URL = "https://github.com/aria2/aria2/releases";
const ARIA2_ARM64_URL =
  "https://github.com/minnyres/aria2-windows-arm64/releases";
</script>

<template>
  <!-- 1) installing -->
  <div
    v-if="banner === 'installing'"
    class="dc-info-banner info"
    role="status"
  >
    <!-- V1 parity (DownloadCenterPanel.js:115): real spinning indicator
         while aria2c is auto-installing. Previously V2 used a static "⏳"
         emoji which gave no animation feedback. The global `.spinner`
         class lives in styles/components/components.css:109. -->
    <span
      class="aria2c-banner__icon"
      aria-hidden="true"
    >
      <span
        class="spinner"
        style="
          width: 14px;
          height: 14px;
          border-width: 2px;
          display: inline-block;
          vertical-align: middle;
        "
      ></span>
    </span>
    <div class="aria2c-banner__body">
      <strong>{{ t("downloads.aria2cInstalling") }}</strong>
      <span>{{ t("downloads.aria2cInstallingDesc") }}</span>
      <code class="aria2c-banner__path">{{
        t("downloads.aria2cInstallPath") + status.bin_dir
      }}</code>
    </div>
  </div>

  <!-- 2) failed -->
  <div
    v-else-if="banner === 'failed'"
    class="dc-info-banner error"
    role="alert"
  >
    <span
      class="aria2c-banner__icon"
      aria-hidden="true"
    >❌</span>
    <div class="aria2c-banner__body">
      <strong>{{ t("downloads.aria2cInstallFailed") }}</strong>
      <span
        v-if="status.install_error"
        class="aria2c-banner__error-detail"
      >{{
        status.install_error
      }}</span>
      <span>{{
        t("downloads.aria2cInstallFailedHint", { dir: status.bin_dir })
      }}</span>
      <a
        :href="ARIA2_RELEASES_URL"
        target="_blank"
        rel="noopener noreferrer"
      >
        {{ t("downloads.aria2cDownloadLink") }}
      </a>
      <!-- V1 parity (DownloadCenterPanel.js:132): "(ARM64 {hint} <a>link</a>)" -->
      <span class="aria2c-banner__arm64">
        (ARM64 {{ t("downloads.aria2cArm64Hint") }}
        <a
          :href="ARIA2_ARM64_URL"
          target="_blank"
          rel="noopener noreferrer"
        >
          {{ t("downloads.aria2cArm64Link") }}
        </a>)
      </span>
    </div>
  </div>

  <!-- 3) available -->
  <div
    v-else-if="banner === 'available'"
    class="dc-info-banner"
    :class="tone"
    role="status"
  >
    <span
      class="aria2c-banner__icon"
      aria-hidden="true"
    >⚡</span>
    <!-- V1 parity (DownloadCenterPanel.js:106-110): inline single-line
         layout — `<strong>Title</strong>（{exe_path}）— {Desc}` then a
         green daemon-running suffix with `(PID: …)`. Previously V2
         flex-wrapped each fragment as its own row and put the path on a
         separate line as a code-chip, which drifted from V1's prose form. -->
    <span class="aria2c-banner__text">
      <strong>{{ t("downloads.aria2cEnabled") }}</strong>
      <template v-if="status.exe_path">
        （<code class="aria2c-banner__path">{{ status.exe_path }}</code>）
      </template>
      — {{ t("downloads.aria2cEnabledDesc") }}
      <span
        v-if="status.daemon_running && status.daemon_pid !== null"
        class="aria2c-banner__pid"
      >
        {{ t("downloads.aria2cDaemonRunning") }} (PID: {{ status.daemon_pid }})
      </span>
    </span>
  </div>

  <!-- 4) can_auto_install -->
  <div
    v-else-if="banner === 'can_auto_install'"
    class="dc-info-banner info"
    role="status"
  >
    <span
      class="aria2c-banner__icon"
      aria-hidden="true"
    >ℹ️</span>
    <!-- V1 parity (DownloadCenterPanel.js:142): aria2cAutoInstallHint is bold,
         not aria2cNotDetected. Layout: plain text + <strong>hint</strong> + suffix. -->
    <div class="aria2c-banner__body">
      <span>{{ t("downloads.aria2cNotDetected") }}<strong>{{ t("downloads.aria2cAutoInstallHint") }}</strong>{{ t("downloads.aria2cAutoInstallSuffix") }}</span>
    </div>
  </div>

  <!-- 5) missing (fallback) -->
  <div
    v-else
    class="dc-info-banner warning"
    role="status"
  >
    <span
      class="aria2c-banner__icon"
      aria-hidden="true"
    >⚠️</span>
    <div class="aria2c-banner__body">
      <span>{{ t("downloads.aria2cMissing") }}</span>
      <a
        :href="ARIA2_RELEASES_URL"
        target="_blank"
        rel="noopener noreferrer"
      >
        {{ t("downloads.aria2cDownloadLink") }}
      </a>
    </div>
  </div>
</template>

<style scoped>
/*
  Banner chrome (display, padding, border, tone colours) is owned by the
  global `.dc-info-banner` rules in `styles/downloads/downloads.css:88-121`
  — V1-aligned 4-tone (info/success/warning/error) palette. We previously
  duplicated the chrome here as `.aria2c-banner.aria2c-banner--*`, which
  drifted from V1 and added a third source of truth. Now we only keep the
  panel-specific BEM children (icon / body / path / arm64 / error-detail).
*/
.aria2c-banner__icon {
  font-size: var(--text-lg);
  line-height: 1.2;
  flex-shrink: 0;
}

.aria2c-banner__body {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-1) var(--space-2);
  font-size: var(--text-sm);
  line-height: 1.45;
  flex: 1 1 auto;
  min-width: 0;
}

/* V1-parity prose-style text container for the `available` banner —
   single inline flow with `<strong>` + path-in-parens + em-dash +
   description + green daemon-running suffix (DownloadCenterPanel.js:106-110).
   Unlike `.aria2c-banner__body`, this does NOT flex-wrap each fragment
   into its own row — it lets the browser word-wrap naturally. */
.aria2c-banner__text {
  font-size: var(--text-sm);
  line-height: 1.45;
  flex: 1 1 auto;
  min-width: 0;
}

.aria2c-banner__text strong {
  font-weight: 600;
}

/* V1 inline `style="color:var(--success)"` (DownloadCenterPanel.js:110) */
.aria2c-banner__pid {
  color: var(--success);
}

.aria2c-banner__body strong {
  font-weight: 600;
}

.aria2c-banner__path,
.aria2c-banner__body code {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  background: var(--bg-code);
  padding: 1px 6px;
  border-radius: 3px;
  word-break: break-all;
}

.aria2c-banner__error-detail {
  color: var(--error);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}

.aria2c-banner__arm64 {
  font-size: var(--text-xs);
  opacity: 0.85;
}

.aria2c-banner__body a {
  color: var(--accent);
  font-weight: 500;
  text-decoration: none;
}

.aria2c-banner__body a:hover {
  text-decoration: underline;
}
</style>
