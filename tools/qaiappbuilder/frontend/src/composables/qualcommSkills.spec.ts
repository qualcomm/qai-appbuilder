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

function panel() {
  let component: any;
  const api = {
    vue: Vue,
    panel: (spec: any) => {
      component = spec.component;
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
      },
    })),
    mcpStatus: vi.fn(async () => ({ enabled: false, servers: [] })),
    setMcpEnabled: vi.fn(async () => ({ enabled: true })),
  };
  activate(api);
  return { api, wrapper: mount(component) };
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
    expect(wrapper.text()).toContain("缺少依赖 missing");
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
