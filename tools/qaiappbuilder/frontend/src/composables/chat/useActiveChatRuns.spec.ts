// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { ref } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const { apiJson, store } = vi.hoisted(() => ({
  apiJson: vi.fn(),
  store: {
    tabs: [] as Array<{
      id: string;
      kind: string;
      conversationId: string | null;
      status: string;
    }>,
    interruptSubAgent: vi.fn(),
    requestCancel: vi.fn(),
    setStreaming: vi.fn(),
    loadHistoryMessages: vi.fn(),
    switchTab: vi.fn(),
    openSubAgentTab: vi.fn(),
    openTab: vi.fn(),
  },
}));
vi.mock("@/api", () => ({ apiJson }));
vi.mock("vue-router", () => ({
  useRouter: () => ({ currentRoute: { value: { name: "chat" } }, push: vi.fn() }),
}));
vi.mock("@/stores/chatTabs", () => ({ useChatTabsStore: () => store }));
vi.mock("@/composables/chat/useChatTransports", () => ({
  useChatTransports: () => ({ peekTransport: () => undefined }),
}));
vi.mock("@/composables/chat/useActiveChatRunAttach", () => ({
  attachActiveChatRun: vi.fn(),
}));

import { useActiveChatRuns, type ActiveRunView } from "./useActiveChatRuns";
describe("useActiveChatRuns", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    apiJson.mockReset();
    apiJson.mockResolvedValue({ items: [] });
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("posts the background-process stop specification and refreshes", async () => {
    const { stopRun } = useActiveChatRuns(ref(false));
    await Promise.resolve();
    apiJson.mockClear();
    const run: ActiveRunView = {
      kind: "background_process",
      id: "bgp_01TEST",
      tab_id: null,
      conversation_id: "conversation-1",
      subagent_id: null,
      root_conversation_id: null,
      title: "Generated app",
      status: "ready",
      model_id: null,
      model_provider: null,
      started_at: "2026-08-26T00:00:00Z",
      last_active_at: "2026-08-26T00:00:00Z",
      aborted: false,
      reason: null,
      openable: false,
      attach_path: null,
      stop: {
        method: "POST",
        path: "/api/background_process/bgp_01TEST/stop",
        body: {},
      },
      localTabId: null,
      isCurrent: false,
      isOpened: false,
      displayTitle: "Generated app",
    };

    await stopRun(run);

    expect(apiJson).toHaveBeenNthCalledWith(1, "POST", run.stop.path, {});
    expect(apiJson).toHaveBeenNthCalledWith(2, "GET", "/api/chat/active-runs");
  });
});
