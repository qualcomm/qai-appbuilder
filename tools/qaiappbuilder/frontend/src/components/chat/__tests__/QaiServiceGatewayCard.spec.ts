// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createI18n } from "vue-i18n";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiJson = vi.fn();
vi.mock("@/api", () => ({ apiJson: (...a: unknown[]) => apiJson(...a) }));
const silentLogin = vi.fn();
const popupLogin = vi.fn();
vi.mock("@/api/auth", () => ({
  silentLogin: (...a: unknown[]) => silentLogin(...a),
  popupLogin: (...a: unknown[]) => popupLogin(...a),
}));
const confirm = vi.fn();
vi.mock("@/composables/useConfirm", () => ({ useConfirm: () => ({ confirm }) }));
vi.mock("@/composables/useToast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }),
}));

import enCloudModels from "@/locales/en/cloudModels";
import QaiServiceGatewayCard from "../QaiServiceGatewayCard.vue";

const OFF = { configured: false, base_url: "", has_token: false, model_count: 0 };
const ON = { configured: true, base_url: "http://gw.test:8012", has_token: true, model_count: 3 };

function mountCard() {
  const i18n = createI18n({
    legacy: false,
    locale: "en",
    messages: { en: { cloudModels: enCloudModels } },
  });
  return mount(QaiServiceGatewayCard, { global: { plugins: [createPinia(), i18n] } });
}

beforeEach(() => {
  setActivePinia(createPinia());
  apiJson.mockReset();
  silentLogin.mockReset();
  popupLogin.mockReset();
  confirm.mockReset();
});

describe("QaiServiceGatewayCard", () => {
  it("starts off: button disabled, no toggle/checkbox anywhere", async () => {
    apiJson.mockResolvedValueOnce(OFF);
    const w = mountCard();
    await flushPromises();
    expect(w.find("input[type=checkbox]").exists()).toBe(false);
    expect(w.find("button").attributes("disabled")).toBeDefined();
    expect(w.text()).toContain("Not connected");
  });

  it("rejects a non-http address (button stays disabled)", async () => {
    apiJson.mockResolvedValueOnce(OFF);
    const w = mountCard();
    await flushPromises();
    await w.find("input").setValue("gw.test:8012");
    expect(w.find("button").attributes("disabled")).toBeDefined();
    expect(w.text()).toContain("http://");
  });

  it("does nothing when the confirmation is declined", async () => {
    apiJson.mockResolvedValueOnce(OFF);
    confirm.mockResolvedValueOnce(false);
    const w = mountCard();
    await flushPromises();
    await w.find("input").setValue("http://gw.test:8012");
    await w.find("button").trigger("click");
    await flushPromises();
    expect(apiJson).toHaveBeenCalledTimes(1); // only the initial GET
    expect(silentLogin).not.toHaveBeenCalled();
  });

  it("confirm -> connect -> silent re-login -> sync", async () => {
    apiJson
      .mockResolvedValueOnce(OFF)
      .mockResolvedValueOnce({ ...OFF, configured: true, base_url: "http://gw.test:8012" })
      .mockResolvedValueOnce({ ...ON, ok: true, skipped_reason: null });
    confirm.mockResolvedValueOnce(true);
    silentLogin.mockResolvedValueOnce(undefined);
    const w = mountCard();
    await flushPromises();
    await w.find("input").setValue("http://gw.test:8012");
    await w.find("button").trigger("click");
    await flushPromises();
    expect(confirm.mock.calls[0]![0].message).toContain("http://gw.test:8012");
    expect(apiJson.mock.calls[1]).toEqual([
      "POST", "/api/qai-service/connect", { url: "http://gw.test:8012" },
    ]);
    expect(silentLogin).toHaveBeenCalledWith({ reexchange: true, timeoutMs: 15000 });
    expect(apiJson.mock.calls[2]![1]).toBe("/api/qai-service/sync");
    expect(w.emitted("changed")).toBeTruthy();
    expect(w.text()).toContain("3 models");
  });

  it("falls back to the popup when the silent re-login fails", async () => {
    apiJson
      .mockResolvedValueOnce(OFF)
      .mockResolvedValueOnce({ ...OFF, configured: true, base_url: "http://gw.test:8012" })
      .mockResolvedValueOnce({ ...ON, ok: true, skipped_reason: null });
    confirm.mockResolvedValueOnce(true);
    silentLogin.mockRejectedValueOnce(new Error("silent_timeout"));
    popupLogin.mockResolvedValueOnce(undefined);
    const w = mountCard();
    await flushPromises();
    await w.find("input").setValue("http://gw.test:8012");
    await w.find("button").trigger("click");
    await flushPromises();
    expect(popupLogin).toHaveBeenCalledWith({ reexchange: true });
    expect(apiJson.mock.calls[2]![1]).toBe("/api/qai-service/sync");
  });

  it("keeps the address and shows a retry hint when sign-in is abandoned", async () => {
    apiJson
      .mockResolvedValueOnce(OFF)
      .mockResolvedValueOnce({ ...OFF, configured: true, base_url: "http://gw.test:8012" });
    confirm.mockResolvedValueOnce(true);
    silentLogin.mockRejectedValueOnce(new Error("silent_timeout"));
    popupLogin.mockRejectedValueOnce(new Error("popup_closed"));
    const w = mountCard();
    await flushPromises();
    await w.find("input").setValue("http://gw.test:8012");
    await w.find("button").trigger("click");
    await flushPromises();
    expect(apiJson).toHaveBeenCalledTimes(2); // no sync, no delete
    expect(w.text()).toContain("press the button to try again");
    expect(w.find("button").text()).toBe("Sign in and sync again");
  });

  it("empty address + stored gateway = disconnect (DELETE)", async () => {
    apiJson.mockResolvedValueOnce(ON).mockResolvedValueOnce(OFF);
    confirm.mockResolvedValueOnce(true);
    const w = mountCard();
    await flushPromises();
    await w.find("input").setValue("");
    expect(w.find("button").text()).toBe("Disconnect");
    await w.find("button").trigger("click");
    await flushPromises();
    expect(apiJson.mock.calls[1]).toEqual(["DELETE", "/api/qai-service"]);
    expect(w.emitted("changed")).toBeTruthy();
  });
});
