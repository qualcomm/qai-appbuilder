// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// Route A: the host loads an extension's ES module at run time and gives it `api`.
// Real `import()` of a data: URL, so the loader's actual code path runs.

import { beforeEach, describe, expect, it, vi } from "vitest";

const apiJson = vi.fn();
vi.mock("@/api/http", () => ({ apiJson: (...a: unknown[]) => apiJson(...a) }));
const toast = { success: vi.fn(), error: vi.fn() };
const uiState = { locale: "en" };
vi.mock("@/composables/useToast", () => ({ useToast: () => toast }));
vi.mock("@/stores/ui", () => ({ useUiStore: () => uiState }));

import {
  __resetExtensionFrontendForTests,
  loadExtensionFrontend,
  resolveLocalizedText,
  setExtensionPromptSender,
  syncExtensionFrontends,
  unloadExtensionFrontend,
  useExtensionFrontend,
} from "../useExtensionFrontend";

/** A unique module URL per source (the module cache is keyed by URL). */
let counter = 0;
function mod(source: string): string {
  counter += 1;
  return `data:text/javascript;charset=utf-8,${encodeURIComponent(`// ${counter}\n${source}`)}`;
}

const fe = useExtensionFrontend();

beforeEach(() => {
  vi.clearAllMocks();
  uiState.locale = "en";
  __resetExtensionFrontendForTests();
});

describe("loading", () => {
  it("imports the module, calls activate(api) once and keeps its panel", async () => {
    const url = mod(`
      export function activate(api) {
        const { h, defineComponent } = api.vue;
        api.panel({ title: "T", component: defineComponent({ render: () => h("i", "x") }) });
        globalThis.__seen = (globalThis.__seen || 0) + 1;
        globalThis.__name = api.name;
      }`);
    await loadExtensionFrontend("demo-ext", url);
    expect(fe.isLoaded("demo-ext")).toBe(true);
    expect(fe.panelOf("demo-ext")?.title).toBe("T");
    expect((globalThis as Record<string, unknown>).__name).toBe("demo-ext");

    await loadExtensionFrontend("demo-ext", url); // same version: nothing runs again
    expect((globalThis as Record<string, unknown>).__seen).toBe(1);
  });

  it("a module without activate, or one that throws, is reported and not loaded", async () => {
    await loadExtensionFrontend("a-ext", mod("export const x = 1;"));
    expect(fe.errorOf("a-ext")).toContain("no activate()");
    expect(fe.isLoaded("a-ext")).toBe(false);

    await loadExtensionFrontend(
      "b-ext",
      mod("export function activate() { throw new Error('boom'); }"),
    );
    expect(fe.errorOf("b-ext")).toBe("boom");
    expect(fe.panelOf("b-ext")).toBeNull();
  });

  it('a bare `import "vue"` is the documented mistake and is reported', async () => {
    await loadExtensionFrontend(
      "c-ext",
      mod('import { h } from "vue"; export function activate() {}'),
    );
    expect(fe.errorOf("c-ext")).not.toBe("");
    expect(fe.isLoaded("c-ext")).toBe(false);
  });

  it("a failed activate runs the dispose callbacks it already registered", async () => {
    (globalThis as Record<string, unknown>).__d = 0;
    await loadExtensionFrontend(
      "d-ext",
      mod(
        "export function activate(api) { api.onDispose(() => { globalThis.__d++; }); throw new Error('x'); }",
      ),
    );
    expect((globalThis as Record<string, unknown>).__d).toBe(1);
  });

  it("api.panel accepts a per-locale title and resolves it with fallbacks", async () => {
    await loadExtensionFrontend(
      "l-ext",
      mod(`export function activate(api) { api.panel({
        title: { en: "Settings", "zh-CN": "设置" },
        component: api.vue.defineComponent({ render: () => null }) }); }`),
    );
    const title = fe.panelOf("l-ext")!.title;
    expect(resolveLocalizedText(title, "zh-CN")).toBe("设置");
    expect(resolveLocalizedText(title, "zh-TW")).toBe("设置");
    expect(resolveLocalizedText(title, "en")).toBe("Settings");
    expect(resolveLocalizedText("Fixed", "zh-CN")).toBe("Fixed");
  });

  it("api.panel rejects a malformed spec", async () => {
    await loadExtensionFrontend(
      "e-ext",
      mod("export function activate(api) { api.panel({ title: 3 }); }"),
    );
    expect(fe.errorOf("e-ext")).toContain("panel(");
    await loadExtensionFrontend(
      "f-ext",
      mod("export function activate(api) { api.panel({ title: {}, component: {} }); }"),
    );
    expect(fe.errorOf("f-ext")).toContain("panel(");
  });
});

describe("reload / unload", () => {
  it("a new URL disposes the old version and takes over", async () => {
    (globalThis as Record<string, unknown>).__disposed = 0;
    const v1 = mod(`export function activate(api) {
      api.onDispose(() => { globalThis.__disposed++; });
      api.panel({ title: "v1", component: api.vue.defineComponent({ render: () => null }) }); }`);
    const v2 = mod(`export function activate(api) {
      api.panel({ title: "v2", component: api.vue.defineComponent({ render: () => null }) }); }`);
    await loadExtensionFrontend("r-ext", v1);
    await loadExtensionFrontend("r-ext", v2);
    expect((globalThis as Record<string, unknown>).__disposed).toBe(1);
    expect(fe.panelOf("r-ext")?.title).toBe("v2");
  });

  it("unload runs dispose once; a throwing dispose does not stop the others", async () => {
    (globalThis as Record<string, unknown>).__u = 0;
    await loadExtensionFrontend(
      "u-ext",
      mod(`export function activate(api) {
        api.onDispose(() => { throw new Error("bad"); });
        api.onDispose(() => { globalThis.__u++; }); }`),
    );
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    unloadExtensionFrontend("u-ext");
    unloadExtensionFrontend("u-ext");
    expect((globalThis as Record<string, unknown>).__u).toBe(1);
    expect(fe.isLoaded("u-ext")).toBe(false);
    warn.mockRestore();
  });

  it("sync loads what the server lists and drops what went away", async () => {
    const a = mod(
      "export function activate(api) { api.panel({ title: 'A', component: api.vue.defineComponent({ render: () => null }) }); }",
    );
    const b = mod(
      "export function activate(api) { api.panel({ title: 'B', component: api.vue.defineComponent({ render: () => null }) }); }",
    );
    await syncExtensionFrontends([
      { name: "a-ext", module: a },
      { name: "b-ext", module: b },
      { name: "n-ext", module: null },
    ]);
    expect(fe.isLoaded("a-ext") && fe.isLoaded("b-ext")).toBe(true);
    expect(fe.isLoaded("n-ext")).toBe(false);

    await syncExtensionFrontends([{ name: "b-ext", module: b }]);
    expect(fe.isLoaded("a-ext")).toBe(false);
    expect(fe.isLoaded("b-ext")).toBe(true);
  });
});

describe("the api handed to the module", () => {
  async function api<T>(body: string): Promise<T> {
    await loadExtensionFrontend(
      "x-ext",
      mod(`export async function activate(api) { globalThis.__api = api; ${body} }`),
    );
    return (globalThis as Record<string, unknown>).__api as T;
  }

  it("fetch goes to this extension's own /api/ext/<name>/ and never outside it", async () => {
    apiJson.mockResolvedValue({ ok: 1 });
    const a = await api<{ fetch: (p: string, i?: object) => Promise<unknown> }>("");
    await a.fetch("/hosts/1", { method: "post", body: { a: 1 } });
    expect(apiJson).toHaveBeenCalledWith("POST", "/api/ext/x-ext/hosts/1", { a: 1 });
    await expect(a.fetch("../other-ext/hosts")).rejects.toThrow("inside the extension");
    await expect(a.fetch("a/../../x")).rejects.toThrow("inside the extension");
  });

  it("settings.get returns the non-secret values only", async () => {
    apiJson.mockResolvedValue({
      settings: [
        { key: "endpoint", type: "text", value: "https://a" },
        { key: "token", type: "secret", is_set: true, value: "LEAK" },
      ],
    });
    const a = await api<{ settings: { get: () => Promise<Record<string, unknown>> } }>("");
    expect(await a.settings.get()).toEqual({ endpoint: "https://a" });
  });

  it("exposes the active locale, toast, sendPrompt and the frozen host vue", async () => {
    const send = vi.fn();
    setExtensionPromptSender(send);
    const a = await api<{
      locale: string;
      toast: { success: (t: string) => void; error: (t: string) => void };
      sendPrompt: (t: string) => void;
      vue: Record<string, unknown>;
    }>("");
    expect(a.locale).toBe("en");
    uiState.locale = "zh-TW";
    expect(a.locale).toBe("zh-TW");
    a.toast.success("ok");
    a.toast.error("bad");
    a.sendPrompt("hello");
    a.sendPrompt("   ");
    expect(toast.success).toHaveBeenCalledWith("ok");
    expect(toast.error).toHaveBeenCalledWith("bad");
    expect(send.mock.calls).toEqual([["hello"]]);
    expect(
      Object.isFrozen(a.vue) && typeof a.vue.h === "function" && typeof a.vue.ref === "function",
    ).toBe(true);
    expect(Object.isFrozen(a)).toBe(true);
    expect(() => {
      "use strict";
      (a as unknown as Record<string, unknown>).name = "other";
    }).toThrow();
  });
});
