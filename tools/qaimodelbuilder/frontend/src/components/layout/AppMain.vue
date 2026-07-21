<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Main content region.
 *
 * S5 PR-052: wraps `<RouterView>` with `<Suspense>` (so views can use
 * `async setup`) and a small inline error boundary. View-level
 * errors propagate as Promise rejections from the API client and are
 * surfaced via each view's own ErrorState; this layer is only a
 * defensive net for unexpected throws.
 */
import { computed, ref } from "vue";
import { RouterView, useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import LoadingState from "@/components/common/LoadingState.vue";
import ErrorState from "@/components/common/ErrorState.vue";

const { t } = useI18n();
const route = useRoute();

// V1 parity (chat.css `.chat-view` + layout.css `.main-content`): the chat
// surface is a fixed-height flex column where the message list scrolls and
// the composer stays pinned to the bottom. The generic `.app-main` wrapper
// (padding + own scroll) would otherwise (a) add an extra 16px gutter around
// the chat panel and (b) make the whole region scroll, pushing the composer
// out of the viewport when there are many messages. So on the chat route we
// switch `.app-main` into a V1-style `.main-content`-equivalent container
// (flex column / overflow hidden / no padding).
//
// NOTE: V1 has no `.app-main` layer at all — every view sits directly under
// `.main-content` (flex column / overflow hidden / padding:0). Per the
// project goal "all views align with V1", `.app-main` is a pure RouterView
// container with no padding of its own: each panel-view supplies its own
// `--space-6` gutter (layout.css `.panel-view`), exactly matching V1's
// `.main-content > .panel-view` layout (no extra 16px outer gutter).
const isChatView = computed(() => {
  return route.path === "/chat" || route.path.startsWith("/chat/");
});

// Service view (like chat) has an internally-scrolling region: the log panel
// must scroll inside its own box while the floating ↑/↓ scroll-nav stays
// pinned. The default `.app-main { overflow-y:auto }` lets the whole region
// scroll instead — the log content stretches `.service-view` past the
// viewport, the panel's own `overflow:auto` never triggers, and the
// absolutely-positioned scroll-nav (anchored to the log body) scrolls away
// with the content. Clamp the height like chat so the inner panel scrolls.
const isFixedHeightView = computed(() => {
  return isChatView.value || route.path.startsWith("/service");
});

const fatalError = ref<Error | null>(null);

function clearError(): void {
  fatalError.value = null;
}
</script>

<template>
  <main
    id="main-content"
    class="app-main"
    :class="{ 'app-main--chat': isChatView, 'app-main--fixed': isFixedHeightView }"
    :aria-label="t('layout.main_aria')"
    role="main"
  >
    <ErrorState
      v-if="fatalError !== null"
      :title="t('error.title')"
      :message="fatalError.message"
      @retry="clearError"
    />
    <RouterView
      v-else
      v-slot="{ Component }"
    >
      <!-- Wrap in a single-root <div> so <Suspense> always sees exactly one
           root node regardless of whether Component is defined yet.
           Vue Router's v-slot can yield undefined during route transitions,
           which would otherwise trigger the "<Suspense> slots expect a single
           root node" experimental warning.

           <KeepAlive> caches each top-level view's component instance so that
           switching away to another nav tab and back PRESERVES all in-memory
           state (scroll position, unsent composer draft, expanded/collapsed
           sections, form edits, etc.) instead of unmounting + re-running
           setup() (which reset everything). Order matters: KeepAlive must wrap
           Suspense (KeepAlive caches the resolved async component; the inner
           <component :is> provides the single cached subtree). -->
      <KeepAlive>
        <Suspense>
          <div class="app-main__view-host">
            <component
              :is="Component"
            />
          </div>
          <template #fallback>
            <LoadingState />
          </template>
        </Suspense>
      </KeepAlive>
    </RouterView>
  </main>
</template>

<style scoped>
.app-main {
  flex: 1;
  min-width: 0;
  min-height: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* V1-parity chat layout: behave like V1 `.main-content` so the chat-view's
   `flex:1` message list scrolls internally and the composer stays pinned to
   the bottom (no extra gutter, no whole-region scroll). */
.app-main--chat {
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
}

/* Fixed-height views (chat + service): clamp `.app-main` to the viewport so
   the inner scrollable region (chat message list / service log panel) scrolls
   inside its own box instead of stretching the whole region. Without this the
   service log panel grows past the viewport, its `overflow:auto` never fires,
   and the floating scroll-nav scrolls away with the content. */
.app-main--fixed {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* The Suspense wrapper div must participate in flex layout so panel-view's
   flex:1 can obtain a constrained height. This lets panel-view become a real
   scrolling container and position:sticky works inside it. */
.app-main__view-host {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}
.app-main--chat .app-main__view-host {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
/* Same flex-column host for service so `.service-view { flex:1 }` can stretch
   to the clamped height and its log panel scrolls internally. */
.app-main--fixed .app-main__view-host {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
</style>
