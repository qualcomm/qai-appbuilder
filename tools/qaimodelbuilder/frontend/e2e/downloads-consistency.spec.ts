import { test, expect } from "@playwright/test";

/**
 * Downloads page — CLI/API/UI consistency.
 *
 * See .junie/plans/qai-webui-e2e-consistency-test.md for the full design.
 * `/downloads` (`src/views/DownloadsView.vue`, a thin wrapper) mounts
 * `DownloadCenterPanel.vue` — visible by DEFAULT, no precondition/overlay
 * gating needed (unlike the App Builder workbench). `DownloadCenterPanel.vue`
 * unconditionally fires `ctx.init()` in its `onMounted` hook, which
 * concurrently triggers ALL of: `GET /api/versions`, `GET
 * /api/versions/local-status`, `GET /api/service-catalog`, `GET
 * /api/service-catalog/local-status`, `GET /api/versions/download-settings`
 * (via `useDownloadSettings.load()`) and `GET /api/aria2c/status` (via
 * `useAria2c`'s `onMounted`). So for every test below the triggering UI
 * action that the intercepted `GET` is attached to is simply `page.goto`
 * itself — no button click / precondition is required first.
 *
 * This page tree has NO `data-testid` attributes anywhere (confirmed by
 * reading every component below in full) — all locators here are CSS
 * class / attribute selectors read directly off `ServiceVersionCard.vue`,
 * `ModelCard.vue` + `ModelCardActions.vue`, `Aria2cBanner.vue` and
 * `DownloadSettingsPanel.vue`.
 *
 * Tab switching: `DownloadCenterPanel.vue` renders a `UiTabs` with
 * `tabs = [{id:"service", label:"⚙️ "+t("downloads.tabService")}, {id:
 * "models", label:"🧠 "+t("downloads.tabModels")}]`. `UiTabs.vue` renders
 * each as a plain `role="tab"` button with NO `data-*` id attribute — only
 * the (locale-dependent) label text. `tabModels` differs per locale
 * ("Local Models" / "本地模型" / "本地模型"), so matching by visible text
 * would be locale-fragile. The tab ARRAY ORDER is fixed by
 * `DownloadCenterPanel.vue`'s `tabs` computed (service first, models
 * second) regardless of locale, so tests below select tabs by index
 * (`.getByRole("tab").nth(0|1)`) scoped to `.downloads-view__tabs`
 * (the `UiTabs` instance's own class) — this is stable across locales.
 *
 * No CSRF is exercised here (unlike `app-builder-consistency.spec.ts`,
 * which POSTs `/api/forge-config` to force a hidden overlay visible): every
 * endpoint covered by this file is a plain `GET`, so `ensureCsrfToken` has
 * no role to play and is intentionally omitted (mirrors
 * `settings-consistency.spec.ts`, the other GET-only consistency file in
 * this directory).
 */

// ─── Minimal wire-shape mirrors (kept local, not imported from `src/`, per
// the established convention in `app-builder-consistency.spec.ts` /
// `settings-consistency.spec.ts` — the e2e project doesn't rely on the
// app's path aliases). See `frontend/src/types/downloads.ts` for the
// authoritative shapes; only the fields these tests actually assert on are
// declared here. ────────────────────────────────────────────────────────

interface ServicePackage {
  platform_id: string;
  [key: string]: unknown;
}

interface ServiceVersion {
  version: string;
  packages: ServicePackage[];
  [key: string]: unknown;
}

interface ServiceVersionsResponse {
  versions: ServiceVersion[];
}

interface ModelVariant {
  variant_id: string;
  [key: string]: unknown;
}

interface CatalogModel {
  model_id: string;
  name: string;
  variants: ModelVariant[];
  [key: string]: unknown;
}

interface CatalogModelsResponse {
  models: CatalogModel[];
}

interface LocalItemStatus {
  downloaded: boolean;
  save_path: string;
  installed: boolean;
  install_path: string;
  [key: string]: unknown;
}

interface VersionsLocalStatus {
  versions: Record<string, LocalItemStatus>;
  [key: string]: unknown;
}

interface ModelsLocalStatus {
  models: Record<string, LocalItemStatus>;
}

type Aria2cInstallStatus = "idle" | "installing" | "done" | "failed";

interface Aria2cStatus {
  available: boolean;
  can_auto_install: boolean;
  exe_path: string;
  daemon_running: boolean;
  daemon_pid: number | null;
  install_status: Aria2cInstallStatus;
  install_error: string;
  bin_dir: string;
}

interface DownloadSettings {
  save_dir: string;
  version_list_url: string;
  catalog_url: string;
  fetch_timeout_seconds: number;
  download_timeout_seconds: number;
  ssl_verify: boolean;
}

type Aria2cBannerState =
  | "available"
  | "installing"
  | "failed"
  | "can_auto_install"
  | "missing";

/**
 * Mirrors `aria2cBannerState()` (`composables/downloads/useAria2c.ts:69-75`)
 * 1:1 — the priority chain that derives the 5-state banner key from the raw
 * status fields. Kept as a plain function here (not imported) per the
 * established e2e convention of not depending on `src/` internals.
 */
function deriveBannerState(s: Aria2cStatus): Aria2cBannerState {
  if (s.install_status === "installing") return "installing";
  if (s.install_status === "failed") return "failed";
  if (s.available) return "available";
  if (s.can_auto_install) return "can_auto_install";
  return "missing";
}

/** Exact-pathname GET matcher — avoids `/api/versions` accidentally also
 * matching `/api/versions/local-status` or `/api/versions/download-settings`
 * (all three share the `/api/versions` prefix). */
function isGetTo(pathname: string) {
  return (r: { url(): string; request(): { method(): string } }) =>
    r.request().method() === "GET" && new URL(r.url()).pathname === pathname;
}

test.describe("Downloads page — CLI/API/UI consistency", () => {
  test.afterEach(async ({}, testInfo) => {
    if (testInfo.status !== testInfo.expectedStatus) {
      await testInfo.attach("defect-record.json", {
        body: JSON.stringify(
          {
            module: "E",
            severity: "major",
            summary: testInfo.title,
            repro: `pnpm exec playwright test -g "${testInfo.title}"`,
            expected: "见测试断言（WebUI 渲染内容应与对应 HTTP API 响应一致）",
            actual: testInfo.error?.message ?? "见 Playwright HTML 报告详情",
            evidence: testInfo.errors.map((e) => e.message).join("\n"),
            discovered_at: new Date().toISOString(),
          },
          null,
          2,
        ),
        contentType: "application/json",
      });
    }
  });

  test("服务版本列表: WebUI 渲染内容与 service-release versions 一致", async ({
    page,
  }) => {
    // UI 人工验证路径：打开 /downloads（默认即停留在 "GenieAPIService" /
    // service 标签，无需任何前置操作）-> 页面挂载即触发 GET /api/versions ->
    // 逐条核对 ServiceVersionCard.vue 渲染出的 article[data-version] 与响应
    // 里的 versions 一一对应，标题（.dc-card__title）包含版本号。
    const responsePromise = page.waitForResponse(isGetTo("/api/versions"));
    await page.goto("/downloads");
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as ServiceVersionsResponse;
    const versions = body.versions;

    // 显式点击一次 "GenieAPIService" 标签（数组下标 0，见文件头注释）：默认即
    // 是该标签，但显式点击可以防止上一个测试残留的 `qai-downloads-tab`
    // localStorage 状态把它漂移到 "models"。
    await page.locator(".downloads-view__tabs").getByRole("tab").nth(0).click();

    const cards = page.locator("article[data-version]");
    await expect(cards).toHaveCount(versions.length);

    for (const v of versions) {
      const card = page.locator(`article[data-version="${v.version}"]`);
      await expect(card).toBeVisible();
      await expect(card.locator(".dc-card__title")).toContainText(v.version);
    }
  });

  test("模型目录: WebUI 渲染内容与 service-release models 一致", async ({
    page,
  }) => {
    // UI 人工验证路径：打开 /downloads -> 页面挂载即并发触发 GET
    // /api/service-catalog（与 versions 请求同一次 init() 里发出，不需要先切
    // 到 "Local Models" 标签才会触发）-> 再点击 "Local Models" 标签（数组下标
    // 1）让 ModelCatalogTab.vue 可见 -> 核对 ModelCard.vue 渲染出的
    // article[data-model] 与响应里的 models 一一对应，标题
    // （.dc-card__title）包含模型名称（model.name）。
    const responsePromise = page.waitForResponse(
      isGetTo("/api/service-catalog"),
    );
    await page.goto("/downloads");
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as CatalogModelsResponse;
    const models = body.models;

    await page.locator(".downloads-view__tabs").getByRole("tab").nth(1).click();

    const cards = page.locator("article[data-model]");
    await expect(cards).toHaveCount(models.length);

    for (const m of models) {
      const card = page.locator(`article[data-model="${m.model_id}"]`);
      await expect(card).toBeVisible();
      await expect(card.locator(".dc-card__title")).toContainText(m.name);
    }
  });

  test("服务版本本地状态: WebUI 渲染内容与 service-release status versions 一致", async ({
    page,
  }) => {
    // UI 人工验证路径：打开 /downloads -> 页面挂载即触发 GET
    // /api/versions/local-status -> 核对每个版本的磁盘派生状态在
    // ServiceVersionCard.vue 上的呈现（实测确认：无实时下载态时只有两种可
    // 展示分支——installed===true 渲染 .dc-card__installed-pill +
    // .dc-card__path（= install_path）；downloaded===true && !installed 渲染
    // .dc-card__save-path-text（= save_path）；两者都为 false 时只有空闲态的
    // 下载按钮，没有任何从 local-status 派生的可断言文本）。
    const responsePromise = page.waitForResponse(
      isGetTo("/api/versions/local-status"),
    );
    await page.goto("/downloads");
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as VersionsLocalStatus;
    const statusByVersion = body.versions;

    await page.locator(".downloads-view__tabs").getByRole("tab").nth(0).click();

    for (const [version, status] of Object.entries(statusByVersion)) {
      const card = page.locator(`article[data-version="${version}"]`);
      // local-status 是磁盘扫描结果，理论上可能包含一个已不在远程 versions
      // 清单里的版本（例如手动装过、后来从 release manifest 移除的版本）——
      // 这种条目在页面上没有对应卡片可核对，跳过而不是断言失败。
      if ((await card.count()) === 0) continue;

      if (status.installed) {
        await expect(card.locator(".dc-card__installed-pill")).toBeVisible();
        await expect(card.locator(".dc-card__path")).toHaveText(
          status.install_path,
        );
      } else if (status.downloaded) {
        await expect(card.locator(".dc-card__save-path-text")).toHaveText(
          status.save_path,
        );
      }
    }
  });

  test("模型本地状态: WebUI 渲染内容与 service-release status models 一致", async ({
    page,
  }) => {
    // UI 人工验证路径：打开 /downloads -> 页面挂载即触发 GET
    // /api/service-catalog/local-status -> 切到 "Local Models" 标签 -> 核对
    // 每个模型的磁盘派生状态在 ModelCard.vue（实际渲染由子组件
    // ModelCardActions.vue 承担）上的呈现——与 ServiceVersionCard.vue 是同一套
    // .dc-card__installed-pill / .dc-card__path / .dc-card__save-path-text
    // 类名约定（两者共享同一套 V1 视觉规范）。
    const responsePromise = page.waitForResponse(
      isGetTo("/api/service-catalog/local-status"),
    );
    await page.goto("/downloads");
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as ModelsLocalStatus;
    const statusByModel = body.models;

    await page.locator(".downloads-view__tabs").getByRole("tab").nth(1).click();

    for (const [modelId, status] of Object.entries(statusByModel)) {
      const card = page.locator(`article[data-model="${modelId}"]`);
      // 同上：local-status 的 key 未必总能直接对应 catalog 的 model_id
      // （useModelCatalog.ts 的 lookupLocalStatus 本身就是三级回退查找），
      // 页面上找不到对应卡片时跳过而不是断言失败。
      if ((await card.count()) === 0) continue;

      if (status.installed) {
        await expect(card.locator(".dc-card__installed-pill")).toBeVisible();
        await expect(card.locator(".dc-card__path")).toHaveText(
          status.install_path,
        );
      } else if (status.downloaded) {
        await expect(card.locator(".dc-card__save-path-text")).toHaveText(
          status.save_path,
        );
      }
    }
  });

  test("aria2c 状态: WebUI 渲染内容与 service-release aria2c status 一致", async ({
    page,
  }) => {
    // UI 人工验证路径：打开 /downloads -> 页面挂载即触发 GET /api/aria2c/status
    // -> Aria2cBanner.vue 按 5 态优先级（installing > failed > available >
    // can_auto_install > missing，见 useAria2c.ts 的 aria2cBannerState()）渲染
    // 唯一一个 .dc-info-banner，色调 class（info/success/error/warning）与该
    // 状态一一对应；installing/failed/available 三态还会把 bin_dir /
    // install_error / exe_path+daemon_pid 具体字段渲染进
    // .aria2c-banner__path / .aria2c-banner__error-detail / .aria2c-banner__pid
    // —— can_auto_install / missing 两态经读码确认只渲染静态 i18n 文案，没有
    // 任何从 status 字段派生的可断言文本，因此这两态只断言色调 class。
    const responsePromise = page.waitForResponse(
      isGetTo("/api/aria2c/status"),
    );
    await page.goto("/downloads");
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const status = (await response.json()) as Aria2cStatus;

    const bannerState = deriveBannerState(status);
    const expectedTone =
      bannerState === "available"
        ? status.daemon_running
          ? "success"
          : "info"
        : bannerState === "installing" || bannerState === "can_auto_install"
          ? "info"
          : bannerState === "failed"
            ? "error"
            : "warning"; // "missing"

    // `.dc-info-banner` is NOT unique to Aria2cBanner.vue — DownloadSettingsPanel.vue
    // conditionally renders its own `.dc-info-banner.warning` (the "save_dir is
    // unsafe" warning, `v-if="isSaveDirUnsafe"`) right after it in DOM order.
    // Aria2cBanner.vue is mounted BEFORE DownloadSettingsPanel.vue in
    // DownloadCenterPanel.vue's template, so `.first()` always resolves to the
    // aria2c banner regardless of whether the settings warning also renders.
    const banner = page.locator(".dc-info-banner").first();
    await expect(banner).toBeVisible();
    await expect(banner).toHaveClass(new RegExp(`\\b${expectedTone}\\b`));

    if (bannerState === "installing") {
      // Aria2cBanner.vue:82-84 — t("downloads.aria2cInstallPath") + bin_dir.
      await expect(banner.locator(".aria2c-banner__path")).toContainText(
        status.bin_dir,
      );
    } else if (bannerState === "failed") {
      if (status.install_error) {
        await expect(
          banner.locator(".aria2c-banner__error-detail"),
        ).toHaveText(status.install_error);
      }
    } else if (bannerState === "available") {
      if (status.exe_path) {
        await expect(banner.locator(".aria2c-banner__path")).toHaveText(
          status.exe_path,
        );
      }
      if (status.daemon_running && status.daemon_pid !== null) {
        await expect(banner.locator(".aria2c-banner__pid")).toContainText(
          String(status.daemon_pid),
        );
      }
    }
    // can_auto_install / missing: 见上方注释，只有色调 class 可验证，已在前面断言。
  });

  test("下载设置: WebUI 渲染内容与 service-release settings get 一致", async ({
    page,
  }) => {
    // UI 人工验证路径：打开 /downloads -> 页面挂载即触发 GET
    // /api/versions/download-settings（DownloadSettingsPanel.vue 的输入框绑定
    // 的正是这份响应，只是 `expanded = ref(false)` 默认折叠 —— 需先点击
    // .dc-settings__toggle 展开面板才能看到 input）-> 核对 5 个原生 id 输入框
    // （#dc-save-dir/#dc-version-url/#dc-catalog-url/#dc-fetch-timeout/
    // #dc-download-timeout；ssl_verify 用的是 ToggleSwitch 组件、没有对应的
    // 原生 input id，题面已明确排除，故不在此断言范围内）。
    const responsePromise = page.waitForResponse(
      isGetTo("/api/versions/download-settings"),
    );
    await page.goto("/downloads");
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const settings = (await response.json()) as DownloadSettings;

    await page.locator(".dc-settings__toggle").click();

    await expect(page.locator("#dc-save-dir")).toHaveValue(settings.save_dir);
    await expect(page.locator("#dc-version-url")).toHaveValue(
      settings.version_list_url,
    );
    await expect(page.locator("#dc-catalog-url")).toHaveValue(
      settings.catalog_url,
    );
    await expect(page.locator("#dc-fetch-timeout")).toHaveValue(
      String(settings.fetch_timeout_seconds),
    );
    await expect(page.locator("#dc-download-timeout")).toHaveValue(
      String(settings.download_timeout_seconds),
    );
  });
});
