// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { flushPromises, mount } from "@vue/test-utils";
import { createI18n } from "vue-i18n";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ExtensionStatus, InstallSummary } from "@/api/extensions";

vi.mock("@/api/extensions", () => ({
  listExtensions: vi.fn(),
  reloadAllExtensions: vi.fn(),
  reloadExtension: vi.fn(),
  setExtensionEnabled: vi.fn(),
  uninstallExtension: vi.fn(),
  getExtensionSettings: vi.fn(),
  saveExtensionSettings: vi.fn(),
  stageExtensionZip: vi.fn(),
  stageExtensionFolder: vi.fn(),
  confirmExtensionInstall: vi.fn(),
  discardExtensionInstall: vi.fn(),
}));
const toast = { success: vi.fn(), error: vi.fn() };
vi.mock("@/composables/useToast", () => ({ useToast: () => toast }));

import * as api from "@/api/extensions";
import enExtensions from "@/locales/en/extensions";
import zhCnExtensions from "@/locales/zh-CN/extensions";
import zhTwExtensions from "@/locales/zh-TW/extensions";
import ExtensionsPanel from "../ExtensionsPanel.vue";

const m = vi.mocked(api);

function ext(over: Partial<ExtensionStatus> = {}): ExtensionStatus {
  return {
    name: "demo-ext", version: "1.0.0", state: "loaded", error: "", tools: ["demo_echo"],
    skills: [], warnings: [], prompts: {}, mcp_servers: [], ...over,
  };
}

function mountPanel() {
  const i18n = createI18n({
    legacy: false,
    locale: "en",
    messages: {
      en: { extensions: enExtensions, common: { loading: "Loading", cancel: "Cancel", save: "Save" } },
    },
  });
  return mount(ExtensionsPanel, { global: { plugins: [i18n] }, attachTo: document.body });
}

beforeEach(() => {
  vi.clearAllMocks();
  document.body.innerHTML = "";
  m.listExtensions.mockResolvedValue({ directory: "C:/data/extensions", extensions: [ext()] });
});

describe("ExtensionsPanel", () => {
  it("lists extensions with their state", async () => {
    m.listExtensions.mockResolvedValue({
      directory: "d",
      extensions: [ext(), ext({ name: "bad-ext", state: "failed", error: "boom", tools: [] })],
    });
    const w = mountPanel();
    await flushPromises();
    expect(w.get('[data-testid="ext-card-demo-ext"]').text()).toContain("Enabled");
    expect(w.get('[data-testid="ext-card-bad-ext"]').text()).toContain("boom");
  });

  it("disables an extension and shows the new state", async () => {
    m.setExtensionEnabled.mockResolvedValue(ext({ state: "disabled" }));
    const w = mountPanel();
    await flushPromises();
    await w.get('[data-testid="ext-toggle-demo-ext"]').trigger("click");
    await flushPromises();
    expect(m.setExtensionEnabled).toHaveBeenCalledWith("demo-ext", false);
    expect(w.get('[data-testid="ext-card-demo-ext"]').text()).toContain("Disabled");
    expect(w.get('[data-testid="ext-toggle-demo-ext"]').text()).toBe("Enable");
  });

  it("stages a zip, shows its capabilities, installs on confirm", async () => {
    const summary: InstallSummary = {
      token: "0123456789abcdef", name: "zip-ext", version: "2.0.0", description: "d",
      runs_code: true, tools: [{ name: "zip_run", group: "command" }], skills: ["s1"],
      prompts: [], settings: [], mcp_servers: [], installed_version: null, replaces: false,
      files: 2, bytes: 10, warnings: [],
    };
    m.stageExtensionZip.mockResolvedValue(summary);
    m.confirmExtensionInstall.mockResolvedValue(ext({ name: "zip-ext", version: "2.0.0" }));
    const w = mountPanel();
    await flushPromises();
    const input = w.get('[data-testid="ext-zip-input"]');
    Object.defineProperty(input.element, "files", { value: [new File(["x"], "z.zip")] });
    await input.trigger("change");
    await flushPromises();

    const dialog = document.querySelector('[data-testid="ext-install-dialog"]');
    expect(dialog?.textContent).toContain("zip_run · command");
    expect(dialog?.textContent).toContain("runs Python code");
    expect(m.confirmExtensionInstall).not.toHaveBeenCalled(); // nothing installed before confirming
    (document.querySelector('[data-testid="ext-install-confirm"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(m.confirmExtensionInstall).toHaveBeenCalledWith("0123456789abcdef", false);
    expect(w.find('[data-testid="ext-card-zip-ext"]').exists()).toBe(true);
  });

  it("uninstall sends the purge choice", async () => {
    m.uninstallExtension.mockResolvedValue(undefined);
    const w = mountPanel();
    await flushPromises();
    await w.get('[data-testid="ext-remove-demo-ext"]').trigger("click");
    await flushPromises();
    const purge = document.querySelector('[data-testid="ext-purge"]') as HTMLInputElement;
    purge.click();
    (document.querySelector('[data-testid="ext-uninstall-confirm"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(m.uninstallExtension).toHaveBeenCalledWith("demo-ext", true);
    expect(w.find('[data-testid="ext-card-demo-ext"]').exists()).toBe(false);
  });

  it("settings: an untouched stored secret is not resent", async () => {
    m.getExtensionSettings.mockResolvedValue([
      { key: "endpoint", type: "text", label: "Endpoint", description: "", required: false, value: "https://a", default: null },
      { key: "token", type: "secret", label: "Token", description: "", required: true, is_set: true },
    ]);
    m.saveExtensionSettings.mockResolvedValue([]);
    const w = mountPanel();
    await flushPromises();
    await w.get('[data-testid="ext-settings-demo-ext"]').trigger("click");
    await flushPromises();
    const secret = document.querySelector('[data-testid="ext-secret-token"]') as HTMLInputElement;
    expect(secret.value).toBe(""); // never filled with the stored value
    (document.querySelector('[data-testid="ext-settings-dialog"]') as HTMLFormElement).dispatchEvent(
      new Event("submit"),
    );
    await flushPromises();
    expect(m.saveExtensionSettings).toHaveBeenCalledWith("demo-ext", { endpoint: "https://a" });
  });
});

describe("extensions locale parity", () => {
  const keys = (o: object, p = ""): string[] =>
    Object.entries(o).flatMap(([k, v]) => (typeof v === "object" ? keys(v, `${p}${k}.`) : [`${p}${k}`]));
  it("zh-CN and zh-TW have exactly the en keys", () => {
    expect(keys(zhCnExtensions).sort()).toEqual(keys(enExtensions).sort());
    expect(keys(zhTwExtensions).sort()).toEqual(keys(enExtensions).sort());
  });
});
