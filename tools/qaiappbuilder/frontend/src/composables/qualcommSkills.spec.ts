import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import * as Vue from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiBytes, apiUpload, releaseAuthGate } from "@/api/http";

const source = readFileSync(
  resolve(process.cwd(), "../factory/extensions/qualcomm-skills/web/index.js"),
  "utf8",
);
// A standalone route-A module has no imports; execute it with the host API.
const activate = new Function(
  `${source.replace("export function activate", "function activate")}; return activate;`,
)();

function panel(preview: Record<string, unknown> = {}) {
  let component: any;
  let title: any;
  const locale = Vue.ref("zh-CN");
  const api = {
    vue: Vue,
    get locale() {
      return locale.value;
    },
    panel: (spec: any) => {
      component = spec.component;
      title = spec.title;
    },
    toast: { success: vi.fn(), error: vi.fn() },
    fetch: vi.fn(async (path: string) => {
      if (path === "packages") return { packages: [] };
      if (path.endsWith("/readme")) return { readme: "<script>unsafe()</script>" };
      if (path === "packages/install")
        return { installed: { id: "demo-1", name: "demo", version: "1", skill_count: 2 } };
      return {};
    }),
    upload: vi.fn(async () => ({
      token: "token",
      preview: {
        name: "demo",
        version: "1",
        skill_count: 2,
        requires: ["dep"],
        missing: ["dep"],
        nesting: ["outer.zip", "inner.zip"],
        readme: "<img src=x onerror=unsafe()>",
        python_ok: true,
        change: "new",
        replaces: [],
        ...preview,
      },
    })),
    mcpStatus: vi.fn(async () => ({ enabled: false, servers: [] })),
    setMcpEnabled: vi.fn(async () => ({ enabled: true })),
  };
  activate(api);
  return { api, locale, title, wrapper: mount(component) };
}

async function click(wrapper: any, label: string) {
  const button = wrapper.findAll("button").find((item: any) => item.text() === label);
  expect(button).toBeDefined();
  await button!.trigger("click");
  await flushPromises();
}

afterEach(() => vi.unstubAllGlobals());

describe("Qualcomm skills route-A panel", () => {
  it("stages raw ZIP, previews safe text, confirms install and enables MCP without reload", async () => {
    const { api, wrapper } = panel();
    await flushPromises();
    const file = new File(["zip"], "outer file.zip", { type: "application/zip" });
    const input = wrapper.get('input[type="file"]');
    Object.defineProperty(input.element, "files", { value: [file], configurable: true });
    await input.trigger("change");
    await flushPromises();
    expect(api.upload).toHaveBeenCalledWith("packages/stage?filename=outer%20file.zip", file);
    expect(wrapper.text()).toContain("inner.zip");
    expect(wrapper.text()).toContain("依赖安装包");
    expect(wrapper.text()).toContain("缺少依赖");
    expect(wrapper.find('[aria-label="安装预览"]').exists()).toBe(true);
    expect(wrapper.get("pre").text()).toBe("<img src=x onerror=unsafe()>");
    expect(wrapper.find("img").exists()).toBe(false);
    expect(api.fetch).not.toHaveBeenCalledWith("packages/install", expect.anything());
    await click(wrapper, "确认安装");
    expect(api.fetch).toHaveBeenCalledWith("packages/install", {
      method: "POST",
      body: { token: "token", allow_network: false },
    });
    expect(wrapper.text()).not.toContain("安装预览");
    await click(wrapper, "一键开启 MCP");
    expect(api.setMcpEnabled).toHaveBeenCalledWith(true);
    expect(api.fetch.mock.calls.some(([path]) => path.includes("reload"))).toBe(false);
    wrapper.unmount();
  });

  async function stageUpload(wrapper: any) {
    await flushPromises();
    const input = wrapper.get('input[type="file"]');
    Object.defineProperty(input.element, "files", {
      value: [new File(["zip"], "demo.zip")],
      configurable: true,
    });
    await input.trigger("change");
    await flushPromises();
  }

  it("a same-version preview explains why and offers no install button", async () => {
    const { api, locale, wrapper } = panel({ change: "same", replaces: ["1"] });
    await stageUpload(wrapper);
    expect(wrapper.get('[data-testid="qc-change-same"]').text()).toContain("已安装，不能重复安装");
    const labels = wrapper.findAll("button").map((b: any) => b.text());
    expect(labels).not.toContain("确认安装");
    expect(labels).toContain("取消");
    locale.value = "en";
    await Vue.nextTick();
    expect(wrapper.get('[data-testid="qc-change-same"]').text()).toContain("already installed");
    expect(api.fetch).not.toHaveBeenCalledWith("packages/install", expect.anything());
    wrapper.unmount();
  });

  it.each([
    ["upgrade", "确认升级", "升级：已安装 1"],
    ["downgrade", "确认降级", "降级：已安装 1"],
  ])(
    "a %s preview is announced at the top and the button says so",
    async (change, button, text) => {
      const { api, wrapper } = panel({ change, replaces: ["1"], version: "2" });
      await stageUpload(wrapper);
      expect(wrapper.get(`[data-testid="qc-change-${change}"]`).text()).toContain(text);
      const section = wrapper.get('[aria-label="安装预览"]');
      expect(section.findAll("h3, p")[1]?.attributes("data-testid")).toBe(`qc-change-${change}`);
      expect(wrapper.findAll("button").map((b: any) => b.text())).not.toContain("确认安装");
      await click(wrapper, button);
      expect(api.fetch).toHaveBeenCalledWith("packages/install", expect.anything());
      wrapper.unmount();
    },
  );

  it("a new package keeps the plain install button and no banner", async () => {
    const { wrapper } = panel();
    await stageUpload(wrapper);
    expect(wrapper.find('[data-testid^="qc-change-"]').exists()).toBe(false);
    expect(wrapper.findAll("button").map((b: any) => b.text())).toContain("确认安装");
    wrapper.unmount();
  });

  it("names the composer button Settings in every UI language", () => {
    const { title, wrapper } = panel();
    expect(title).toEqual({ en: "Settings", "zh-CN": "设置", "zh-TW": "設定" });
    wrapper.unmount();
  });

  it("renders the preview in the active language and switches when locale changes", async () => {
    const { locale, wrapper } = panel();
    await flushPromises();
    const input = wrapper.get('input[type="file"]');
    Object.defineProperty(input.element, "files", {
      value: [new File(["zip"], "demo.zip")],
      configurable: true,
    });
    await input.trigger("change");
    await flushPromises();
    expect(wrapper.text()).toContain("依赖安装包");
    expect(wrapper.text()).toContain("安装预览");
    locale.value = "en";
    await Vue.nextTick();
    expect(wrapper.text()).toContain("Installation preview");
    expect(wrapper.text()).toContain("Required packages");
    expect(wrapper.find('[aria-label="Installation preview"]').exists()).toBe(true);
    expect(wrapper.get("details").get("summary").text()).toBe("README (plain text)");
    wrapper.unmount();
  });

  it("collapses preview README by default and cancels without installing", async () => {
    const { api, wrapper } = panel();
    await flushPromises();
    const input = wrapper.get('input[type="file"]');
    Object.defineProperty(input.element, "files", {
      value: [new File(["zip"], "demo.zip")],
      configurable: true,
    });
    await input.trigger("change");
    await flushPromises();
    const details = wrapper.get("details");
    expect((details.element as HTMLDetailsElement).open).toBe(false);
    expect(details.get("summary").text()).toBe("README（纯文本）");
    // Native details toggling is handled by the browser, not jsdom.
    (details.element as HTMLDetailsElement).open = true;
    await wrapper.get('input[type="checkbox"]').setValue(true);
    expect((wrapper.get("details").element as HTMLDetailsElement).open).toBe(true);
    await click(wrapper, "取消");
    expect(wrapper.find('[aria-label="安装预览"]').exists()).toBe(false);
    expect(api.fetch).not.toHaveBeenCalledWith("packages/install", expect.anything());
    await input.trigger("change");
    await flushPromises();
    expect((wrapper.get("details").element as HTMLDetailsElement).open).toBe(false);
    expect((wrapper.get('input[type="checkbox"]').element as HTMLInputElement).checked).toBe(false);
    wrapper.unmount();
  });

  it("reads installed README as text and requires confirmation before deleting", async () => {
    const { api, wrapper } = panel();
    api.fetch.mockImplementation(async (path: string) =>
      path === "packages"
        ? { packages: [{ id: "demo-1", name: "demo", version: "1" }] }
        : path.endsWith("/readme")
          ? { readme: "<script>unsafe()</script>" }
          : ({} as any),
    );
    await flushPromises();
    await click(wrapper, "刷新列表与状态");
    await click(wrapper, "README");
    expect(wrapper.get("pre").text()).toBe("<script>unsafe()</script>");
    expect(wrapper.find("script").exists()).toBe(false);
    await click(wrapper, "删除");
    expect(api.fetch).not.toHaveBeenCalledWith("packages/demo-1", { method: "DELETE" });
    await click(wrapper, "确认删除");
    expect(api.fetch).toHaveBeenCalledWith("packages/demo-1", { method: "DELETE" });
    wrapper.unmount();
  });
});

describe("raw ZIP HTTP helper", () => {
  it("shares upload handling but sends a Blob with ZIP content type, not FormData", async () => {
    releaseAuthGate();
    const fetchMock = vi.fn(async () => new Response('{"token":"ok"}', { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const blob = new Blob(["ZIP"]);
    expect(
      await apiBytes("/api/ext/qualcomm-skills/packages/stage", blob, {
        headers: { "Content-Type": "wrong" },
      }),
    ).toEqual({ token: "ok" });
    const request = fetchMock.mock.calls[0] as any;
    expect(request[1].body).toBe(blob);
    expect(request[1].headers.get("Content-Type")).toBe("application/zip");
    expect(request[1].credentials).toBe("same-origin");
    expect(request[1].method).toBe("POST");
    const form = new FormData();
    await apiUpload("/upload", form, { headers: { "Content-Type": "wrong" } });
    const multipart = fetchMock.mock.calls[1] as any;
    expect(multipart[1].body).toBe(form);
    expect(multipart[1].headers.has("Content-Type")).toBe(false);
  });
});
