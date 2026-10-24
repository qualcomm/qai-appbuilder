// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { flushPromises, mount } from "@vue/test-utils";
import { createI18n } from "vue-i18n";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BuilderEnvJob, BuilderEnvStatus } from "@/api/builderEnv";

vi.mock("@/api/builderEnv", () => ({
  fetchBuilderEnv: vi.fn(),
  startBuilderEnvInstall: vi.fn(),
  cancelBuilderEnvInstall: vi.fn(),
}));

const push = vi.fn();
vi.mock("vue-router", () => ({ useRouter: () => ({ push }) }));

import * as api from "@/api/builderEnv";
import enBuilderEnv from "@/locales/en/builderEnv";
import zhCnBuilderEnv from "@/locales/zh-CN/builderEnv";
import { __resetBuilderEnvForTests } from "@/composables/useBuilderEnv";
import BuilderEnvBanner from "../BuilderEnvBanner.vue";
import BuilderEnvCard from "../../downloads/BuilderEnvCard.vue";

const fetchMock = vi.mocked(api.fetchBuilderEnv);
const startMock = vi.mocked(api.startBuilderEnvInstall);
const cancelMock = vi.mocked(api.cancelBuilderEnvInstall);

function job(over: Partial<BuilderEnvJob> = {}): BuilderEnvJob {
  return {
    job_id: "j1", state: "running", overall: 42, step: "sdk_download", step_state: "running",
    message: "", unit: "bytes", done: 1_073_741_824, total: 2_147_483_648, speed: 5_242_880, eta: 200,
    log_tail: [], error_code: "", error_message: "", failed_step: "", started_at: 1, finished_at: null,
    ...over,
  };
}

function status(over: Partial<BuilderEnvStatus> = {}): BuilderEnvStatus {
  return {
    ready: false, missing: ["sdk", "venv"],
    problems: [{ code: "sdk_missing", path: "C:\\x", key: "" }],
    sdk_root: "C:\\Qualcomm\\AIStack\\QAIRT\\2.48", venv: "C:\\v", config_file: "c.json",
    job: null, installable: true, ...over,
  };
}

function i18n(locale: "en" | "zh-CN" = "en") {
  return createI18n({
    legacy: false,
    locale,
    fallbackLocale: "en",
    messages: {
      en: { builderEnv: enBuilderEnv, common: { close: "Close" } },
      "zh-CN": { builderEnv: zhCnBuilderEnv, common: { close: "关闭" } },
    },
  });
}

async function mountCard(s: BuilderEnvStatus, locale: "en" | "zh-CN" = "en") {
  fetchMock.mockResolvedValue(s);
  const w = mount(BuilderEnvCard, { global: { plugins: [i18n(locale)] } });
  await flushPromises();
  return w;
}

async function mountBanner(s: BuilderEnvStatus) {
  fetchMock.mockResolvedValue(s);
  const w = mount(BuilderEnvBanner, { global: { plugins: [i18n()] } });
  await flushPromises();
  return w;
}

const tid = (id: string) => `[data-testid="${id}"]`;

beforeEach(() => {
  __resetBuilderEnvForTests();
  fetchMock.mockReset();
  startMock.mockReset();
  cancelMock.mockReset();
  push.mockReset();
  window.sessionStorage.clear();
});

afterEach(() => {
  __resetBuilderEnvForTests();
});

describe("BuilderEnvCard", () => {
  it("is hidden until the state is known, and when the server has no such feature", async () => {
    fetchMock.mockRejectedValue(Object.assign(new Error("nf"), { status: 404 }));
    const w = mount(BuilderEnvCard, { global: { plugins: [i18n()] } });
    await flushPromises();
    expect(w.find(tid("builder-env-card")).exists()).toBe(false);
  });

  it("missing: explains, shows what is missing and offers Install", async () => {
    const w = await mountCard(status());
    expect(w.get(tid("builder-env-card")).attributes("data-phase")).toBe("missing");
    expect(w.text()).toContain("not installed");
    expect(w.get(tid("builder-env-missing")).text()).toContain("QAIRT SDK");
    expect(w.get(tid("builder-env-install")).text()).toBe("Install");
    expect(w.find(tid("builder-env-cancel")).exists()).toBe(false);
  });

  it("Install sends the chosen mirror and proxy", async () => {
    startMock.mockResolvedValue({ job_id: "j1", status: "queued" });
    const w = await mountCard(status());
    await w.get(tid("builder-env-options-toggle")).trigger("click");
    await w.get(tid("builder-env-mirror")).setValue("cn");
    await w.get(tid("builder-env-proxy")).setValue("http://127.0.0.1:7890");
    await w.get(tid("builder-env-install")).trigger("click");
    await flushPromises();
    expect(startMock).toHaveBeenCalledWith({ mirror: "cn", proxy: "http://127.0.0.1:7890" });
  });

  it("defaults: auto mirror, no proxy", async () => {
    startMock.mockResolvedValue({ job_id: "j1", status: "queued" });
    const w = await mountCard(status());
    await w.get(tid("builder-env-install")).trigger("click");
    await flushPromises();
    expect(startMock).toHaveBeenCalledWith({ mirror: "auto", proxy: "" });
  });

  it("an invalid proxy blocks Install and says why", async () => {
    const w = await mountCard(status());
    await w.get(tid("builder-env-options-toggle")).trigger("click");
    await w.get(tid("builder-env-proxy")).setValue("127.0.0.1:7890");
    expect(w.find(tid("builder-env-proxy-invalid")).exists()).toBe(true);
    expect(w.get(tid("builder-env-install")).attributes("disabled")).toBeDefined();
    await w.get(tid("builder-env-install")).trigger("click");
    expect(startMock).not.toHaveBeenCalled();
  });

  it("installing: progress bar, step, size / speed / ETA and Cancel; no Install button", async () => {
    const w = await mountCard(status({ job: job() }));
    expect(w.get(tid("builder-env-card")).attributes("data-phase")).toBe("installing");
    expect(w.get(tid("builder-env-progress")).attributes("aria-valuenow")).toBe("42");
    expect(w.get(".progress-bar").attributes("style")).toContain("width: 42%");
    const text = w.text();
    expect(text).toContain("Downloading QAIRT SDK");
    expect(text).toContain("1.0 GB / 2.0 GB");
    expect(text).toContain("5.0 MB/s");
    expect(text).toContain("ETA 3m 20s");
    expect(text).toContain("keep using the app");
    expect(w.find(tid("builder-env-install")).exists()).toBe(false);
    expect(w.find(tid("builder-env-options-toggle")).exists()).toBe(false);
  });

  it("Cancel calls the API", async () => {
    cancelMock.mockResolvedValue({ cancelled: true });
    const w = await mountCard(status({ job: job() }));
    await w.get(tid("builder-env-cancel")).trigger("click");
    await flushPromises();
    expect(cancelMock).toHaveBeenCalledTimes(1);
  });

  it("percent-unit steps (unpacking) show a percentage, phase steps show n/total", async () => {
    let w = await mountCard(status({ job: job({ step: "sdk_extract", unit: "percent", done: 63, total: 100, speed: 0, eta: null }) }));
    expect(w.text()).toContain("63%");
    __resetBuilderEnvForTests();
    w = await mountCard(status({ job: job({ step: "deps", unit: "phase", done: 1, total: 2, speed: 0, eta: null }) }));
    expect(w.text()).toContain("1/2");
  });

  it("failed: shows a human reason, the failed step, a log toggle and Retry", async () => {
    const w = await mountCard(
      status({
        job: job({
          state: "failed", error_code: "network", error_message: "raw message", failed_step: "sdk_download",
          log_tail: ["attempt 1/5 failed", "attempt 5/5 failed"],
        }),
      }),
    );
    expect(w.get(tid("builder-env-error")).text()).toContain("Network error while downloading");
    expect(w.text()).toContain("Downloading QAIRT SDK");
    expect(w.get(tid("builder-env-install")).text()).toBe("Retry");
    expect(w.find(tid("builder-env-log")).exists()).toBe(false);
    await w.get(tid("builder-env-log-toggle")).trigger("click");
    expect(w.get(tid("builder-env-log")).text()).toContain("attempt 5/5 failed");
  });

  it("an unknown error code falls back to the backend's message", async () => {
    const w = await mountCard(status({ job: job({ state: "failed", error_code: "exit_7", error_message: "installer exited with code 7" }) }));
    expect(w.get(tid("builder-env-error")).text()).toContain("installer exited with code 7");
  });

  it("cancelled: soft wording and Retry", async () => {
    const w = await mountCard(status({ job: job({ state: "cancelled", error_code: "cancelled" }) }));
    expect(w.get(tid("builder-env-card")).attributes("data-phase")).toBe("cancelled");
    expect(w.text()).toContain("cancelled");
    expect(w.find(tid("builder-env-error")).exists()).toBe(false);
    expect(w.get(tid("builder-env-install")).text()).toBe("Retry");
  });

  it("ready: green state with the SDK path, no buttons", async () => {
    const w = await mountCard(status({ ready: true, missing: [], problems: [] }));
    expect(w.get(tid("builder-env-card")).attributes("data-phase")).toBe("ready");
    expect(w.text()).toContain("ready");
    expect(w.text()).toContain("C:\\Qualcomm\\AIStack\\QAIRT\\2.48");
    expect(w.find("button").exists()).toBe(false);
  });

  it("unavailable build: explains and offers no Install button", async () => {
    const w = await mountCard(status({ installable: false }));
    expect(w.get(tid("builder-env-card")).attributes("data-phase")).toBe("unavailable");
    expect(w.text()).toContain("cannot install it automatically");
    expect(w.find(tid("builder-env-install")).exists()).toBe(false);
  });

  it("shows a start error from the API", async () => {
    startMock.mockRejectedValue(new Error("server said no"));
    const w = await mountCard(status());
    await w.get(tid("builder-env-install")).trigger("click");
    await flushPromises();
    expect(w.get(tid("builder-env-action-error")).text()).toBe("server said no");
  });

  it("renders in Chinese", async () => {
    const w = await mountCard(status({ job: job({ state: "failed", error_code: "disk_space", failed_step: "deps" }) }), "zh-CN");
    expect(w.text()).toContain("模型转换环境");
    expect(w.get(tid("builder-env-error")).text()).toContain("磁盘可用空间不足");
    expect(w.text()).toContain("Python 依赖包");
  });
});

describe("BuilderEnvBanner", () => {
  it.each(["unknown-404"])("is invisible when the feature is absent (%s)", async () => {
    fetchMock.mockRejectedValue(Object.assign(new Error("nf"), { status: 404 }));
    const w = mount(BuilderEnvBanner, { global: { plugins: [i18n()] } });
    await flushPromises();
    expect(w.find(tid("builder-env-banner")).exists()).toBe(false);
  });

  it("is invisible when ready and when the build cannot install it", async () => {
    let w = await mountBanner(status({ ready: true, missing: [], problems: [] }));
    expect(w.find(tid("builder-env-banner")).exists()).toBe(false);
    __resetBuilderEnvForTests();
    w = await mountBanner(status({ installable: false }));
    expect(w.find(tid("builder-env-banner")).exists()).toBe(false);
  });

  it("missing: warns and links to the Model Converter tab of /downloads", async () => {
    const w = await mountBanner(status());
    expect(w.get(tid("builder-env-banner")).attributes("data-phase")).toBe("missing");
    expect(w.get(tid("builder-env-banner-text")).text()).toContain("not installed yet");
    await w.get(tid("builder-env-banner-go")).trigger("click");
    expect(push).toHaveBeenCalledWith({ path: "/downloads", query: { tab: "converter" } });
  });

  it("installing: shows the percentage, cannot be dismissed, action says 'View progress'", async () => {
    const w = await mountBanner(status({ job: job({ overall: 37 }) }));
    expect(w.get(tid("builder-env-banner-text")).text()).toContain("37%");
    expect(w.get(tid("builder-env-banner-go")).text()).toBe("View progress");
    expect(w.find(tid("builder-env-banner-dismiss")).exists()).toBe(false);
  });

  it("failed and cancelled each get their own wording", async () => {
    let w = await mountBanner(status({ job: job({ state: "failed" }) }));
    expect(w.get(tid("builder-env-banner-text")).text()).toContain("failed");
    __resetBuilderEnvForTests();
    w = await mountBanner(status({ job: job({ state: "cancelled" }) }));
    expect(w.get(tid("builder-env-banner-text")).text()).toContain("cancelled");
  });

  it("dismiss hides it for the session only and remembers across remounts", async () => {
    const w = await mountBanner(status());
    await w.get(tid("builder-env-banner-dismiss")).trigger("click");
    expect(w.find(tid("builder-env-banner")).exists()).toBe(false);
    expect(window.sessionStorage.getItem("qai.builderEnv.bannerDismissed.v1")).toBe("1");
    __resetBuilderEnvForTests();
    const again = await mountBanner(status());
    expect(again.find(tid("builder-env-banner")).exists()).toBe(false);
  });

  it("a dismissed banner still reappears while an install is running", async () => {
    window.sessionStorage.setItem("qai.builderEnv.bannerDismissed.v1", "1");
    const w = await mountBanner(status({ job: job() }));
    expect(w.find(tid("builder-env-banner")).exists()).toBe(true);
  });

  it("works when sessionStorage is unavailable", async () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
    const w = await mountBanner(status());
    expect(w.find(tid("builder-env-banner")).exists()).toBe(true);
    await w.get(tid("builder-env-banner-dismiss")).trigger("click");
    expect(w.find(tid("builder-env-banner")).exists()).toBe(false);
    spy.mockRestore();
    set.mockRestore();
  });
});
