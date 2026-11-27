// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// Built-in tool modes and user-extension modes are mutually exclusive per tab:
// entering a built-in mode (any path that calls setActiveMode) leaves the
// tab's extension mode; leaving a built-in mode (null) does not touch it.

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api", () => ({ apiJson: vi.fn() }));

import {
  __resetExtensionModesForTests,
  activeExtensionsFor,
  useExtensionModes,
} from "@/composables/useExtensionModes";
import { useChatTabsStore } from "@/stores/chatTabs";

beforeEach(() => {
  setActivePinia(createPinia());
  __resetExtensionModesForTests();
});

describe("setActiveMode vs extension modes", () => {
  it("entering a built-in mode leaves only that tab's extension mode", () => {
    const store = useChatTabsStore();
    store.restoreLayout({
      version: 1,
      tabs: [{ conversationId: "c1", title: "a" }, { conversationId: "c2", title: "b" }],
      activeIndex: 0,
    });
    const [t1, t2] = store.tabs.map((t) => t.id);
    const modes = useExtensionModes();
    modes.select(t1!, "remote-host");
    modes.select(t2!, "remote-host");

    store.setActiveMode(t1!, null); // leaving a built-in mode: no effect
    expect(activeExtensionsFor(t1)).toEqual(["remote-host"]);

    store.setActiveMode(t1!, "app-builder");
    expect(activeExtensionsFor(t1)).toEqual([]);
    expect(activeExtensionsFor(t2)).toEqual(["remote-host"]);
  });
});
