// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { flushPromises, mount } from "@vue/test-utils";
import { createI18n } from "vue-i18n";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiJson = vi.fn();
vi.mock("@/api/http", () => ({ apiJson: (...a: unknown[]) => apiJson(...a) }));
const push = vi.fn();
vi.mock("vue-router", () => ({ useRouter: () => ({ push }) }));
const toast = { success: vi.fn(), error: vi.fn() };
vi.mock("@/composables/useToast", () => ({ useToast: () => toast }));

import enExtensions from "@/locales/en/extensions";
import {
  __resetExtensionModesForTests,
  activeExtensionsFor,
  clearExtensionMode,
  useExtensionModes,
} from "@/composables/useExtensionModes";
import ComposerExtensionMenu from "../ComposerExtensionMenu.vue";

const ENTRY = {
  name: "remote-host",
  label: "Remote Host",
  icon: "🖥",
  description: "d",
  actions: [
    {
      id: "authorize",
      label: "Authorize host",
      description: "",
      fields: [
        { key: "host", type: "text", label: "Host", description: "", required: true, default: null },
        { key: "password", type: "secret", label: "Password", description: "", required: false },
      ],
    },
  ],
};

const mounted: { unmount: () => void }[] = [];
afterEach(() => {
  mounted.splice(0).forEach((w) => w.unmount());
});

function mountMenu(tabId = "tab-1") {
  const i18n = createI18n({
    legacy: false,
    locale: "en",
    messages: { en: { extensions: enExtensions, common: { cancel: "Cancel" } } },
  });
  const w = mount(ComposerExtensionMenu, {
    props: { tabId, conversationId: "conv-1" },
    slots: { default: '<button data-testid="default-mode-button">Model Builder</button>' },
    global: { plugins: [i18n] },
    attachTo: document.body,
  });
  mounted.push(w);
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  document.body.innerHTML = "";
  __resetExtensionModesForTests();
  apiJson.mockImplementation((method: string, path: string) =>
    method === "GET" && path === "/api/extensions/ui"
      ? Promise.resolve({ extensions: [ENTRY] })
      : Promise.reject(new Error(`unexpected ${method} ${path}`)),
  );
});

describe("ComposerExtensionMenu", () => {
  it("menu -> pick -> mode tag + action buttons; exit leaves the mode", async () => {
    const w = mountMenu();
    await flushPromises();
    expect(w.find('[data-testid="default-mode-button"]').exists()).toBe(true);
    expect(w.get('[data-testid="ext-mode"]').classes()).toContain("rit-left");
    await w.get('[data-testid="ext-menu-btn"]').trigger("click");
    await flushPromises();
    await w.get('[data-testid="ext-menu-item-remote-host"]').trigger("click");

    expect(w.get('[data-testid="ext-mode-exit"]').text()).toContain("Remote Host");
    expect(w.find('[data-testid="default-mode-button"]').exists()).toBe(false);
    expect(w.find('[data-testid="ext-mode-exit"] .rit-close').exists()).toBe(true);
    expect(w.find('[data-testid="ext-action-authorize"]').exists()).toBe(true);
    expect(activeExtensionsFor("tab-1")).toEqual(["remote-host"]);
    expect(activeExtensionsFor("tab-2")).toEqual([]); // per tab

    await w.get('[data-testid="ext-mode-exit"]').trigger("click");
    expect(activeExtensionsFor("tab-1")).toEqual([]);
    expect(w.find('[data-testid="ext-menu-btn"]').exists()).toBe(true);
    expect(w.find('[data-testid="default-mode-button"]').exists()).toBe(true);
  });

  it("action dialog posts the values and sends the returned prompt", async () => {
    const w = mountMenu();
    await flushPromises();
    await w.get('[data-testid="ext-menu-btn"]').trigger("click");
    await flushPromises();
    await w.get('[data-testid="ext-menu-item-remote-host"]').trigger("click");
    await w.get('[data-testid="ext-action-authorize"]').trigger("click");

    apiJson.mockResolvedValueOnce({ ok: true, message: "authorized", prompt: "check host h1" });
    const host = document.querySelector('[data-testid="ext-action-field-host"]') as HTMLInputElement;
    host.value = "h1";
    host.dispatchEvent(new Event("input"));
    (document.querySelector('[data-testid="ext-action-dialog"]') as HTMLFormElement).dispatchEvent(
      new Event("submit"),
    );
    await flushPromises();

    expect(apiJson).toHaveBeenLastCalledWith(
      "POST",
      "/api/extensions/remote-host/actions/authorize",
      { values: { host: "h1", password: null }, conversation_id: "conv-1", tab_id: "tab-1" },
    );
    expect(toast.success).toHaveBeenCalledWith("authorized");
    expect(w.emitted("send-prompt")).toEqual([["check host h1"]]);
    expect(document.querySelector('[data-testid="ext-action-dialog"]')).toBeNull();
  });

  it("an extension error keeps the dialog open and shows the message", async () => {
    const w = mountMenu();
    await flushPromises();
    await w.get('[data-testid="ext-menu-btn"]').trigger("click");
    await flushPromises();
    await w.get('[data-testid="ext-menu-item-remote-host"]').trigger("click");
    await w.get('[data-testid="ext-action-authorize"]').trigger("click");
    apiJson.mockResolvedValueOnce({ ok: false, message: "RuntimeError: no route", prompt: "" });
    (document.querySelector('[data-testid="ext-action-dialog"]') as HTMLFormElement).dispatchEvent(
      new Event("submit"),
    );
    await flushPromises();
    expect(toast.error).toHaveBeenCalledWith("RuntimeError: no route");
    expect(document.querySelector('[data-testid="ext-action-dialog"]')).not.toBeNull();
    expect(w.emitted("send-prompt")).toBeUndefined();
  });

  it("picking an extension emits entered; clearExtensionMode leaves the mode", async () => {
    const w = mountMenu();
    await flushPromises();
    await w.get('[data-testid="ext-menu-btn"]').trigger("click");
    await flushPromises();
    await w.get('[data-testid="ext-menu-item-remote-host"]').trigger("click");
    expect(w.emitted("entered")).toEqual([["remote-host"]]); // parent leaves built-in modes
    clearExtensionMode("tab-1"); // what the tabs store does when a built-in mode is entered
    await flushPromises();
    expect(activeExtensionsFor("tab-1")).toEqual([]);
    expect(w.find('[data-testid="ext-menu-btn"]').exists()).toBe(true);
  });

  it("a selected extension that disappears ends its mode", async () => {
    const w = mountMenu();
    await flushPromises();
    await w.get('[data-testid="ext-menu-btn"]').trigger("click");
    await flushPromises();
    await w.get('[data-testid="ext-menu-item-remote-host"]').trigger("click");
    expect(activeExtensionsFor("tab-1")).toEqual(["remote-host"]);

    // Uninstalled elsewhere; the next refresh (any menu, any tab) drops the mode.
    apiJson.mockResolvedValueOnce({ extensions: [] });
    await useExtensionModes().refreshEntries();
    await flushPromises();
    expect(activeExtensionsFor("tab-1")).toEqual([]);
    expect(w.find('[data-testid="ext-menu-btn"]').exists()).toBe(true);
  });
});
