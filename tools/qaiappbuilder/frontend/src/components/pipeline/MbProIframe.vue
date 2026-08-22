<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
import { computed, ref, watch, onMounted, onUnmounted, nextTick } from "vue";
import { useProConnection } from "@/composables/useProConnection";

const props = defineProps<{ tabId: string }>();
const { isConnectedFor, agentUrlFor, sessionIdFor } = useProConnection();

const iframeSrc = computed(() => {
  if (!isConnectedFor(props.tabId)) return "";
  const base = agentUrlFor(props.tabId);
  const sid  = sessionIdFor(props.tabId);
  if (!base || !sid) return "";
  return `${base.replace(/\/+$/, "")}/${encodeURIComponent(sid)}?compact=1`;
});

const iframeHeight = ref(300);

const iframeRef = ref<HTMLIFrameElement | null>(null);

function requestHeight(): void {
  iframeRef.value?.contentWindow?.postMessage({ type: "mbp_request_height" }, "*");
}

function onMessage(evt: MessageEvent): void {
  if (!evt.data || evt.data.type !== "mbp_height") return;
  const h = Number(evt.data.height);
  if (isNaN(h) || h < 40) return;
  iframeHeight.value = h;
}

// Publish the iframe's bottom edge so top-right floating anchors
// (chat.css `.chat-view__intro-anchor`) can offset themselves and
// avoid being occluded by the iframe. Cleared on unmount.
function _findChatView(el: HTMLElement | null): HTMLElement | null {
  let node: HTMLElement | null = el;
  while (node !== null) {
    if (node.classList && node.classList.contains("chat-view")) return node;
    node = node.parentElement;
  }
  return null;
}
function _publishIframeBottom(): void {
  const host = _findChatView(iframeRef.value);
  if (host === null) return;
  host.style.setProperty("--qai-mbp-iframe-bottom", `${iframeHeight.value}px`);
}
function _clearIframeBottom(): void {
  const host = _findChatView(iframeRef.value);
  if (host === null) return;
  host.style.removeProperty("--qai-mbp-iframe-bottom");
}

watch(iframeHeight, () => {
  void nextTick(_publishIframeBottom);
});

onMounted(() => {
  window.addEventListener("message", onMessage);
  void nextTick(_publishIframeBottom);
});
onUnmounted(() => {
  window.removeEventListener("message", onMessage);
  _clearIframeBottom();
});
</script>

<template>
  <!-- sandbox rationale: allow-scripts + allow-same-origin together is
       intentional even though the combination grants the framed page full
       access to its origin's cookies/storage. The framed origin is the
       trusted MB Pro Agent (we ship & control it), and mbp's UI needs both
       script execution and same-origin storage for its own session state.
       allow-forms is for the composer, allow-popups for the "open in tab"
       link, allow-downloads for artifact exports. Comment kept here so
       future security scans / reviewers see the decision was deliberate. -->
  <iframe
    v-if="iframeSrc !== ''"
    ref="iframeRef"
    :src="iframeSrc"
    class="mbpp-iframe"
    :style="{ height: `${iframeHeight}px` }"
    referrerpolicy="no-referrer"
    sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-downloads"
    @load="requestHeight"
  ></iframe>
</template>

<style scoped>
.mbpp-iframe {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  width: 100%;
  border: none;
  border-bottom: 1px solid var(--border);
  background: #0a0e1a;
  z-index: 1;
}
</style>
