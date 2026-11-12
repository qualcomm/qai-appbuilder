// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { flushPromises, mount } from "@vue/test-utils";
import { createI18n } from "vue-i18n";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiJson = vi.fn();
vi.mock("@/api", () => ({ apiJson: (...a: unknown[]) => apiJson(...a) }));
vi.mock("@/composables/useToast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }),
}));

import enCloudModels from "@/locales/en/cloudModels";
import CloudModelQuickAdd from "../CloudModelQuickAdd.vue";

function mountQuick(providers: unknown[] = []) {
  const i18n = createI18n({
    legacy: false,
    locale: "en",
    messages: { en: { cloudModels: enCloudModels, common: { close: "Close" } } },
  });
  return mount(CloudModelQuickAdd, { props: { providers: providers as never }, global: { plugins: [i18n] } });
}
const btn = (w: ReturnType<typeof mountQuick>, text: string) =>
  w.findAll("button").find((b) => b.text().includes(text))!;

beforeEach(() => apiJson.mockReset());

describe("CloudModelQuickAdd", () => {
  it("prefills the preset Base URL and locks it; Custom unlocks", async () => {
    const w = mountQuick();
    const url = w.find("input.quick-input");
    expect((url.element as HTMLInputElement).value).toBe("https://api.deepseek.com/v1");
    expect(url.attributes("readonly")).toBeDefined();
    await btn(w, "Zhipu").trigger("click");
    expect((w.find("input.quick-input").element as HTMLInputElement).value).toBe(
      "https://open.bigmodel.cn/api/paas/v4",
    );
    await btn(w, "Custom").trigger("click");
    expect(w.find("input.quick-input").attributes("readonly")).toBeUndefined();
  });

  it("fetch -> tick -> add PUTs one provider doc with auto context and the key", async () => {
    apiJson
      .mockResolvedValueOnce({
        ok: true, status: 200, code: null,
        model_ids: ["deepseek-chat", "deepseek-reasoner"],
        context_lengths: { "deepseek-chat": 128000, "deepseek-reasoner": 128000 },
      })
      .mockResolvedValueOnce({});
    const w = mountQuick();
    await w.findAll("input.quick-input")[1]!.setValue("sk-test");
    await btn(w, "Fetch models").trigger("click");
    await flushPromises();
    expect(apiJson.mock.calls[0]).toEqual([
      "POST", "/api/model-catalog/providers/probe",
      { base_url: "https://api.deepseek.com/v1", api_key: "sk-test" },
    ]);
    await w.findAll("input[type=checkbox]")[0]!.setValue(true);
    await btn(w, "Add 1 model").trigger("click");
    await flushPromises();
    const [method, path, body] = apiJson.mock.calls[1]!;
    expect([method, path]).toEqual(["PUT", "/api/model-catalog/providers/deepseek"]);
    expect(body.config.api_key).toBe("sk-test");
    expect(body.config.base_url).toBe("https://api.deepseek.com/v1");
    expect(body.config.models).toEqual([
      { model_id: "deepseek-chat", name: "deepseek-chat", supports_streaming: true, context_length: 128000 },
    ]);
    // No sampling parameters are ever written by the quick-add path.
    expect(JSON.stringify(body)).not.toMatch(/temperature|top_p|max_tokens|params/);
    expect(w.emitted("saved")).toBeTruthy();
  });

  it("maps a 401 to an actionable hint and still allows a manual model id", async () => {
    apiJson
      .mockResolvedValueOnce({ ok: false, status: 401, code: "unauthorized", model_ids: [], context_lengths: {} })
      .mockResolvedValueOnce({});
    const w = mountQuick();
    await btn(w, "Fetch models").trigger("click");
    await flushPromises();
    expect(w.text()).toContain("rejected the API key");
    await w.findAll("input.quick-input").at(-1)!.setValue("deepseek-chat");
    await btn(w, "Use this ID").trigger("click");
    await btn(w, "Add 1 model").trigger("click");
    await flushPromises();
    const body = apiJson.mock.calls[1]![2];
    expect(body.config.models).toEqual([
      { model_id: "deepseek-chat", name: "deepseek-chat", supports_streaming: true },
    ]); // no context_length: the backend falls back to its family table
  });

  it("merges into an existing provider and skips models already added", async () => {
    apiJson
      .mockResolvedValueOnce({ ok: true, status: 200, code: null, model_ids: ["a", "b"], context_lengths: {} })
      .mockResolvedValueOnce({});
    const w = mountQuick([
      { provider_id: "deepseek", config: { base_url: "https://api.deepseek.com/v1", pinned: true, models: [{ model_id: "a" }] } },
    ]);
    await btn(w, "Fetch models").trigger("click");
    await flushPromises();
    const boxes = w.findAll("input[type=checkbox]");
    expect(boxes[0]!.attributes("disabled")).toBeDefined();
    await boxes[1]!.setValue(true);
    await btn(w, "Add 1 model").trigger("click");
    await flushPromises();
    const cfg = apiJson.mock.calls[1]![2].config;
    expect(cfg.pinned).toBe(true);
    expect(cfg.models.map((m: { model_id: string }) => m.model_id)).toEqual(["a", "b"]);
    expect(cfg).not.toHaveProperty("api_key"); // blank key => keep the stored one
  });

  it("derives a provider id from the host for Custom", async () => {
    apiJson.mockResolvedValueOnce({});
    const w = mountQuick();
    await btn(w, "Custom").trigger("click");
    await w.find("input.quick-input").setValue("https://llm.corp.example.com:8443/v1");
    await w.findAll("input.quick-input").at(-1)!.setValue("my-model");
    await btn(w, "Use this ID").trigger("click");
    await btn(w, "Add 1 model").trigger("click");
    await flushPromises();
    expect(apiJson.mock.calls[0]![1]).toBe("/api/model-catalog/providers/custom-llm-corp-example-com");
  });
});
