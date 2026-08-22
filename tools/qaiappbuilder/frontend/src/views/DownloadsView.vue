<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  DownloadsView — thin shell that mounts <DownloadCenterPanel>.

  Two routes mount the same panel:
    /downloads  — V2 sidebar entry (`AppSidebar.vue` "Downloads" item)
    /updates    — V1 nav-key parity (preserved so deep links from chat
                  status messages, e.g. "Genie not installed → /updates",
                  keep working).

  All state lives in the singleton orchestrator returned by
  `useDownloadsStore` (which wraps `useDownloadCenter`); both routes
  see the same in-flight downloads / aria2c poll / settings.
-->
<script setup lang="ts">
import DownloadCenterPanel from "@/components/chat/DownloadCenterPanel.vue";
</script>

<template>
  <div class="downloads-view-shell">
    <DownloadCenterPanel />
  </div>
</template>

<style scoped>
/*
  The Download Center tab strip is rendered by the shared <UiTabs
  variant="underline"> inside <DownloadCenterPanel>, identical to the
  Settings page tabs — so the active underline is drawn by UiTabs itself
  and needs no per-view override here. (A previous container
  `border-bottom` + child negative-margin combo made the active underline
  collapse onto the container rule and disappear; removing it and reusing
  the standard underline variant fixes it and keeps all page-level tabs on
  one implementation.)
*/
.downloads-view-shell {
  display: contents;
}
</style>
