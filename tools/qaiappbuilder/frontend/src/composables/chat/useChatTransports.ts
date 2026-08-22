// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useChatTransports — app-level (singleton) per-tab transport manager.
 *
 * Multi-session parallelism: each open chat tab owns its OWN
 * {@link ChatTransport} (strict isolation — refactor-plan §10.6 inv 2).
 * Previously the per-tab transport `Map` lived as a local variable inside
 * `ChatView.vue`, so leaving the `/chat` route unmounted ChatView and its
 * `onBeforeUnmount` disposed every transport — killing any background tab
 * that was still streaming.
 *
 * V1 parity (useChat.js:507-508 / 759-760 / 2760-2763): switching away from a
 * streaming conversation must NOT abort it — the request keeps running in the
 * background and writes its result back when it finishes. To get that across
 * route changes the transport cache must outlive the ChatView component.
 *
 * This module hoists the cache to module scope (one instance per app) and only
 * disposes a transport when the tab is genuinely closed (the tab disappears
 * from `chatTabsStore.tabs`). Navigating away from `/chat` no longer tears
 * transports down, so background tabs keep streaming.
 *
 * The manager is reactive-aware: it watches the live tab id list once (lazily,
 * on first access) and prunes transports for closed tabs. The watcher is never
 * stopped — it is an app-lifetime singleton, mirroring the Pinia store it
 * tracks.
 */
import { watch } from "vue";
import {
  useChatTabsStore,
  type TabId,
} from "@/stores/chatTabs";
import {
  useChatTransport,
  type ChatTransport,
} from "@/composables/chat/useChatTransport";

/**
 * Module-level transport cache. One {@link ChatTransport} per open tab,
 * keyed by the client-side local tab id. Lives for the lifetime of the app
 * (NOT a component), so background streams survive route changes.
 */
const transports = new Map<TabId, ChatTransport>();

/** Guard so the prune watcher is installed exactly once. */
let pruneWatcherInstalled = false;

/**
 * Test-only transport factory override. Production leaves this `null` so
 * every tab's transport is the real {@link useChatTransport} (WS-first /
 * SSE-fallback default preserved). Integration tests inject a fully-controlled
 * fake transport via {@link _setChatTransportFactory} so a re-sent / dequeued
 * turn does NOT open a real WebSocket or SSE `fetch` (which would hit the
 * network, fail async, and flip the tab to `error` — and whose pending readers
 * would bleed across tests). Never read in production.
 */
let testTransportFactory: ((tabId: TabId) => ChatTransport) | null = null;

export interface ChatTransportsManager {
  /**
   * Return the transport for `tabId`, creating it on first use. The
   * transport is cached and reused for the lifetime of the tab.
   */
  getTransport(tabId: TabId): ChatTransport;
  /**
   * Return the transport for `tabId` only if one already exists, else
   * `undefined`. Use this for cancel / inspect paths that must NOT lazily
   * create a transport.
   */
  peekTransport(tabId: TabId): ChatTransport | undefined;
  /**
   * Explicitly dispose + drop the transport for `tabId`. Normally not
   * needed — closed tabs are pruned automatically — but exposed for tests
   * and defensive teardown.
   */
  disposeTransport(tabId: TabId): void;
}

/**
 * Install (once) a watcher that prunes transports whose tab has been closed.
 * Disposing a closed tab's transport releases its underlying WS / fetch.
 * A still-open tab is never pruned even if ChatView is unmounted, so a
 * background stream keeps running across route changes.
 */
function ensurePruneWatcher(): void {
  if (pruneWatcherInstalled) {
    return;
  }
  pruneWatcherInstalled = true;
  const store = useChatTabsStore();
  watch(
    () => store.tabs.map((t) => t.id),
    (ids) => {
      const idSet = new Set(ids);
      for (const [tabId, transport] of transports.entries()) {
        if (!idSet.has(tabId)) {
          transport.dispose();
          transports.delete(tabId);
        }
      }
    },
  );
}

/**
 * Access the app-level transport manager. Safe to call from any component;
 * the underlying cache + prune watcher are singletons.
 */
export function useChatTransports(): ChatTransportsManager {
  ensurePruneWatcher();

  function getTransport(tabId: TabId): ChatTransport {
    const existing = transports.get(tabId);
    if (existing !== undefined) {
      return existing;
    }
    // `testTransportFactory` is null in production (real transport, WS-first
    // default preserved); tests inject a fully-controlled fake so a re-sent
    // turn neither opens a real WebSocket nor an SSE `fetch`.
    const fresh =
      testTransportFactory !== null
        ? testTransportFactory(tabId)
        : useChatTransport({ tabId });
    transports.set(tabId, fresh);
    return fresh;
  }

  function disposeTransport(tabId: TabId): void {
    const transport = transports.get(tabId);
    if (transport !== undefined) {
      transport.dispose();
      transports.delete(tabId);
    }
  }

  function peekTransport(tabId: TabId): ChatTransport | undefined {
    return transports.get(tabId);
  }

  return { getTransport, peekTransport, disposeTransport };
}

/** Test-only reset hook — disposes & clears all cached transports. */
export function _resetChatTransports(): void {
  for (const transport of transports.values()) {
    transport.dispose();
  }
  transports.clear();
  pruneWatcherInstalled = false;
  testTransportFactory = null;
}

/**
 * Test-only hook: override how a tab's transport is created. Integration tests
 * inject a fully-controlled fake {@link ChatTransport} so a re-sent / dequeued
 * turn neither opens a real WebSocket nor an SSE `fetch` — making the test
 * deterministic and free of pending-reader cross-test pollution. Pass `null`
 * to restore the production factory. No effect on transports already cached —
 * call this BEFORE the code under test creates one (or after a
 * {@link _resetChatTransports}). Never used in production code.
 */
export function _setChatTransportFactory(
  factory: ((tabId: TabId) => ChatTransport) | null,
): void {
  testTransportFactory = factory;
}
