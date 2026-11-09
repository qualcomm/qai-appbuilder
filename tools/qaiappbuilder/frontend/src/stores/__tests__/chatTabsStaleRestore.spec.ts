// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// A tab layout restored from localStorage can name conversations that the backend no longer has (reinstall with an
// empty DB that kept the WebView storage). Such untouched tabs must disappear instead of lingering as ghost tabs.

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiJson = vi.fn();
vi.mock("@/api", () => ({
  apiJson: (...args: unknown[]) => apiJson(...args),
}));

import { useChatTabsStore } from "@/stores/chatTabs";

const notFound = Object.assign(new Error("not found"), { status: 404, code: "chat.conversation_not_found" });
const serverError = Object.assign(new Error("boom"), { status: 500, code: "internal" });

/** `alive` conversations answer with an empty page; every other id fails with `failure`. */
function backendWith(alive: string[], failure: Error = notFound): void {
  apiJson.mockImplementation(async (_method: string, path: string) => {
    const id = decodeURIComponent(path.split("?")[0]!.split("/")[4] ?? "");
    if (!alive.includes(id)) {
      throw failure;
    }
    return path.includes("/messages") ? { items: [], next_cursor: null } : { message_count: 0 };
  });
}

function restore(ids: string[]) {
  const store = useChatTabsStore();
  store.restoreLayout({
    version: 1,
    tabs: ids.map((id, i) => ({ conversationId: id, title: `t${i}` })),
    activeIndex: 0,
  });
  return store;
}

/** Load every restored tab's history, one after another (as ChatView does, minus the parallelism: the mocked
 *  dynamic `import("@/api")` does not resolve for concurrent callers under vitest). */
async function loadAll(store: ReturnType<typeof useChatTabsStore>): Promise<void> {
  for (const id of store.tabs.map((t) => t.id)) {
    await store.loadHistoryMessages(id);
  }
}

describe("restored tabs whose conversation is gone", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    apiJson.mockReset();
  });

  it("closes them all and leaves one blank tab", async () => {
    backendWith([]);
    const store = restore(["dead1", "dead2", "dead3"]);
    expect(store.tabs).toHaveLength(3);
    await loadAll(store);
    expect(store.tabs).toHaveLength(1);
    expect(store.tabs[0]?.conversationId ?? null).toBeNull();
    expect(store.activeTabId).toBe(store.tabs[0]?.id);
  });

  it("keeps the tabs whose conversation still exists", async () => {
    backendWith(["alive"]);
    const store = restore(["dead1", "alive", "dead2"]);
    await loadAll(store);
    expect(store.tabs.map((t) => t.conversationId)).toEqual(["alive"]);
  });

  it("does not drop a tab on a transient server error", async () => {
    backendWith([], serverError);
    const store = restore(["c1", "c2"]);
    await loadAll(store);
    expect(store.tabs.map((t) => t.conversationId)).toEqual(["c1", "c2"]);
  });

  it("only unbinds (does not close) a tab the user already typed into", async () => {
    backendWith([]);
    const store = restore(["dead1"]);
    const id = store.tabs[0]!.id;
    store.$patch((s) => {
      s.tabs[0]!.status = "streaming";
    });
    await store.loadHistoryMessages(id);
    expect(store.tabs.map((t) => t.id)).toEqual([id]);
    expect(store.tabs[0]!.conversationId).toBeNull();
  });
});
