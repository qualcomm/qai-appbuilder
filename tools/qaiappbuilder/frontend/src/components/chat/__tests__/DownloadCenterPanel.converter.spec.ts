// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * The model-conversion environment lives in its own Download Center tab ("converter").
 * The panel's other children and its store are stubbed: only the tab wiring is under test.
 */
import { flushPromises, mount } from "@vue/test-utils";
import { computed, defineComponent, h, ref } from "vue";
import { createI18n } from "vue-i18n";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { BuilderEnvStatus } from "@/api/builderEnv";

vi.mock("@/api/builderEnv", () => ({
  fetchBuilderEnv: vi.fn(),
  startBuilderEnvInstall: vi.fn(),
  cancelBuilderEnvInstall: vi.fn(),
}));

const route = { query: {} as Record<string, unknown> };
vi.mock("vue-router", () => ({ useRoute: () => route, useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/composables/useHeaderActions", () => ({ useHeaderActions: vi.fn() }));

// A minimal orchestrator: just the fields the panel reads.
const activeTab = ref<string>("service");
const fetchVersions = vi.fn();
const fetchCatalog = vi.fn();
const setActiveTab = vi.fn((t: string) => {
  activeTab.value = t;
});
vi.mock("@/stores/downloads", () => ({
  useDownloadsStore: () => ({
    activeTab,
    setActiveTab,
    init: vi.fn(),
    isAnyServiceDownloading: ref(false),
    isAnyModelDownloading: ref(false),
    modelCatalog: { modelCount: ref(0), loading: ref(false), fetchCatalog },
    serviceVersions: { loading: ref(false), fetchVersions },
    aria2c: { status: ref(null), banner: ref(null) },
    settings: { settings: ref({}), saving: ref(false), patch: vi.fn() },
    saveDownloadSettings: vi.fn(),
    activeDownloads: computed(() => []),
    downloads: ref({}),
  }),
}));
vi.mock("@/composables/useDownloadCenter", () => ({ provideDownloadCenter: vi.fn() }));

import * as api from "@/api/builderEnv";
import { __resetBuilderEnvForTests } from "@/composables/useBuilderEnv";
import enBuilderEnv from "@/locales/en/builderEnv";
import DownloadCenterPanel from "../DownloadCenterPanel.vue";

const fetchMock = vi.mocked(api.fetchBuilderEnv);

const stub = (name: string) => defineComponent({ name, setup: () => () => h("div", { "data-stub": name }) });

const UiTabsStub = defineComponent({
  name: "UiTabs",
  props: { modelValue: String, tabs: { type: Array, default: () => [] } },
  setup(props) {
    return () =>
      h(
        "div",
        { "data-testid": "tabs" },
        (props.tabs as Array<{ id: string; label: string; badge?: string | number }>).map((x) =>
          h("span", { "data-tab": x.id, "data-badge": x.badge === undefined ? "" : String(x.badge) }, x.label),
        ),
      );
  },
});

function status(over: Partial<BuilderEnvStatus> = {}): BuilderEnvStatus {
  return {
    ready: false, missing: ["sdk"], problems: [], sdk_root: "", venv: "", config_file: "",
    job: null, installable: true, ...over,
  };
}

function i18n() {
  return createI18n({
    legacy: false,
    locale: "en",
    messages: {
      en: {
        builderEnv: enBuilderEnv,
        common: { refresh: "Refresh", close: "Close" },
        index: { clearFinished: "Clear" },
        downloads: {
          title: "Downloads", checkUpdate: "Check",
          tabService: "GenieAPIService", tabModels: "Local Models", tabConverter: "Model Converter",
          subtitleService: "svc", subtitleModels: "mdl", subtitleConverter: "cvt",
          converterUnknown: "Checking the model-conversion environment...",
        },
      },
    },
  });
}

async function mountPanel(s: BuilderEnvStatus | Error, tab = "service") {
  if (s instanceof Error) fetchMock.mockRejectedValue(s);
  else fetchMock.mockResolvedValue(s);
  activeTab.value = tab;
  const w = mount(DownloadCenterPanel, {
    global: {
      plugins: [i18n()],
      stubs: {
        UiTabs: UiTabsStub,
        Aria2cBanner: stub("Aria2cBanner"),
        ActiveDownloadsBar: stub("ActiveDownloadsBar"),
        DownloadSettingsPanel: stub("DownloadSettingsPanel"),
        ServiceVersionsTab: stub("ServiceVersionsTab"),
        ModelCatalogTab: stub("ModelCatalogTab"),
        BuilderEnvCard: stub("BuilderEnvCard"),
      },
    },
  });
  await flushPromises();
  return w;
}

const stubEl = (w: ReturnType<typeof mount>, n: string) => w.find(`[data-stub="${n}"]`);
// `isVisible()` reports true in happy-dom for detached roots, so read what v-show actually wrote.
const hidden = (w: ReturnType<typeof mount>, n: string) => /display:\s*none/.test(stubEl(w, n).attributes("style") ?? "");

beforeEach(() => {
  __resetBuilderEnvForTests();
  fetchMock.mockReset();
  setActiveTab.mockClear();
  fetchVersions.mockClear();
  fetchCatalog.mockClear();
  route.query = {};
});

describe("DownloadCenterPanel - Model Converter tab", () => {
  it("has three tabs: service, models, converter", async () => {
    const w = await mountPanel(status());
    const ids = w.findAll("[data-tab]").map((e) => e.attributes("data-tab"));
    expect(ids).toEqual(["service", "models", "converter"]);
    expect(w.get('[data-tab="converter"]').text()).toContain("Model Converter");
  });

  it("the card is shown only on the converter tab", async () => {
    for (const [tab, shown] of [["service", false], ["models", false], ["converter", true]] as const) {
      const w = await mountPanel(status(), tab);
      expect(hidden(w, "BuilderEnvCard"), tab).toBe(!shown);
      w.unmount();
    }
  });

  it("the download-only chrome (aria2c banner, settings, active bar) is hidden on the converter tab", async () => {
    const w = await mountPanel(status(), "converter");
    for (const n of ["Aria2cBanner", "DownloadSettingsPanel", "ActiveDownloadsBar"]) {
      expect(stubEl(w, n).exists(), n).toBe(false);
    }
    w.unmount();
    const w2 = await mountPanel(status(), "service");
    for (const n of ["Aria2cBanner", "DownloadSettingsPanel", "ActiveDownloadsBar"]) {
      expect(stubEl(w2, n).exists(), n).toBe(true);
    }
  });

  it("the other two tabs stay mounted (their state is kept) but hidden on the converter tab", async () => {
    const w = await mountPanel(status(), "converter");
    expect(stubEl(w, "ServiceVersionsTab").exists()).toBe(true);
    expect(hidden(w, "ServiceVersionsTab")).toBe(true);
    expect(stubEl(w, "ModelCatalogTab").exists()).toBe(true);
    expect(hidden(w, "ModelCatalogTab")).toBe(true);
  });

  it("?tab=converter deep-links to the tab; unknown values are ignored", async () => {
    route.query = { tab: "converter" };
    await mountPanel(status());
    expect(setActiveTab).toHaveBeenCalledWith("converter");
    setActiveTab.mockClear();
    route.query = { tab: "bogus" };
    await mountPanel(status());
    expect(setActiveTab).not.toHaveBeenCalled();
  });

  it("badge: pulsing dot while installing, '!' after a failure, nothing when idle or ready", async () => {
    const badge = async (s: BuilderEnvStatus) => {
      const w = await mountPanel(s);
      const b = w.get('[data-tab="converter"]').attributes("data-badge");
      w.unmount();
      __resetBuilderEnvForTests();
      return b;
    };
    const job = (state: string) => ({ job_id: "j", state, overall: 5 }) as unknown as BuilderEnvStatus["job"];
    expect(await badge(status({ job: job("running") }))).toBe("\u25cf");
    expect(await badge(status({ job: job("failed") }))).toBe("!");
    expect(await badge(status({ job: job("cancelled") }))).toBe("!");
    expect(await badge(status())).toBe("");
    expect(await badge(status({ ready: true, missing: [] }))).toBe("");
  });

  it("the header refresh re-reads the converter status on that tab, not the service versions", async () => {
    const w = await mountPanel(status(), "converter");
    fetchMock.mockClear();
    const { useHeaderActions } = await import("@/composables/useHeaderActions");
    const factory = vi.mocked(useHeaderActions).mock.calls.at(-1)![0] as () => Array<{ id: string; onClick: () => void }>;
    factory().find((a) => a.id === "downloads.refresh")!.onClick();
    await flushPromises();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchVersions).not.toHaveBeenCalled();
    expect(fetchCatalog).not.toHaveBeenCalled();
    w.unmount();
  });

  it("unknown state (still loading / feature absent) shows a hint instead of a blank tab", async () => {
    const w = await mountPanel(new Error("offline"), "converter");
    expect(w.find('[data-testid="converter-tab-empty"]').exists()).toBe(true);
    const other = await mountPanel(new Error("offline"), "service");
    expect(other.find('[data-testid="converter-tab-empty"]').exists()).toBe(false);
  });

  it("polls only while the panel is mounted (acquire / release)", async () => {
    const w = await mountPanel(status());
    const calls = fetchMock.mock.calls.length;
    expect(calls).toBeGreaterThanOrEqual(1);
    w.unmount();
    vi.useFakeTimers();
    await vi.advanceTimersByTimeAsync(120_000);
    vi.useRealTimers();
    expect(fetchMock.mock.calls.length).toBe(calls);
  });
});
