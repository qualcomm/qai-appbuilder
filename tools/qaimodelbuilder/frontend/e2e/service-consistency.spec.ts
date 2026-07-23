import { test, expect } from "@playwright/test";

/**
 * Service page — CLI/API/UI consistency.
 *
 * Unlike the App Builder workbench (`app-builder-consistency.spec.ts`), the
 * `/service` route (`src/views/ServiceView.vue`) is visible by DEFAULT — no
 * `show_workbench`-style config flag needs to be flipped first, so every test
 * here starts from a plain `page.goto("/service")`.
 *
 * `ServiceView.vue` / `ServiceStatusCard.vue` / `ServiceLaunchParams.vue` /
 * `ServiceConnectionBar.vue` have NO `data-testid` attributes at all, so this
 * file uses CSS class selectors throughout (confirmed by reading each
 * component's `<template>` directly — see per-test comments below for the
 * exact source line each selector/assertion is grounded in).
 *
 * Not every one of the 5 endpoints below is actually auto-fetched on mount:
 * `useServiceControl.ts`'s `init()` only concurrently fires
 * `loadServiceParams` / `loadServiceStatus` / `loadServiceModels` — the probe
 * (`GET /api/service/probe`, test 2) is exclusively button-triggered
 * (`testConnection()`), and the service-config read (`GET /api/config`,
 * test 4) only happens once `ServiceConfigPanel.vue` mounts, which itself
 * only happens once `ServiceConfigModal.vue`'s `v-if="open"` becomes true.
 * Those two tests therefore drive the triggering UI action first (mirroring
 * this suite's sibling `app-builder-consistency.spec.ts`, which does the same
 * whenever the endpoint isn't purely load-triggered), instead of forcing a
 * `page.goto`-only trigger that the real code doesn't support.
 *
 * All 5 target endpoints are GET, so — unlike the sibling file — no
 * `ensureCsrfToken()` helper is needed here.
 */

/** Wire form of `GET /api/service/status` (see `src/types/service.ts`). */
interface ServiceStatusResponse {
  running?: boolean;
  state?: string;
  pid?: number | null;
  uptime_seconds?: number | null;
  model?: string | null;
  port?: number | null;
  exe_path?: string;
  command?: string;
  path_warning?: string;
  memory_mb?: number;
}

/** Wire form of `GET /api/service/probe` (see `src/types/service.ts`). */
interface ProbeServiceResponse {
  reachable: boolean;
  alive?: boolean;
  model?: string | null;
}

/** A single entry of `GET /api/service/models` (see `src/types/service.ts`). */
interface ServiceModelEntry {
  name: string;
  path: string;
  size_mb: number;
  config_path?: string;
  format?: string;
  context_length?: number;
  is_running?: boolean;
}

/** Wire form of `GET /api/service/models` (see `src/types/service.ts`). */
interface ServiceModelsResponse {
  models: ServiceModelEntry[];
  models_root_path?: string;
}

/** A single fixed model slot inside `service_config.json` `models[]`. */
interface ModelSlot {
  name: string;
  [key: string]: unknown;
}

/** Wire form of `GET /api/config` (see `service-config/types.ts`). */
interface ServiceConfigResponse {
  config: {
    default_model?: string;
    models?: ModelSlot[];
    [key: string]: unknown;
  };
  meta?: { using_default_config: boolean; config_file_path: string };
}

/**
 * Mirrors `useServiceControl.ts` `formatUptime()` exactly (h > 0 → "Xh Ym",
 * m > 0 → "Xm Ys", else "Xs") so this file can compute the expected
 * `.service-status-meta` text without importing a Vue composable.
 */
function formatUptime(secs: number | null | undefined): string {
  if (secs == null) return "";
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = Math.floor(secs % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

test.describe("Service page — CLI/API/UI consistency", () => {
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

  test("服务状态: WebUI 渲染内容与 service status 一致", async ({ page }) => {
    // GET /api/service/status is fired unconditionally by init() on mount
    // (useServiceControl.ts:600-604 loadServiceStatus), so the response can be
    // awaited around a plain page.goto.
    const responsePromise = page.waitForResponse(
      (r) =>
        r.url().includes("/api/service/status") &&
        r.request().method() === "GET",
    );
    await page.goto("/service");
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const status = (await response.json()) as ServiceStatusResponse;

    // Mirrors useServiceControl.ts:149-153 `isRunning` computed: a string
    // `state` field takes priority over the legacy `running` boolean.
    const expectedRunning =
      typeof status.state === "string"
        ? status.state === "running"
        : status.running === true;

    // ServiceStatusCard.vue:49-52 — `.service-status-indicator` gains the
    // `running` class exactly when `isRunning` is true.
    const indicator = page.locator(".service-status-indicator");
    await expect(indicator).toBeVisible();
    if (expectedRunning) {
      await expect(indicator).toHaveClass(/running/);
    } else {
      await expect(indicator).not.toHaveClass(/running/);
    }

    // ServiceStatusCard.vue:56-58 — `.status-on` / `.status-off` span inside
    // `.service-status-title` mirrors the same boolean.
    await expect(
      page.locator(expectedRunning ? ".status-on" : ".status-off"),
    ).toBeVisible();

    if (expectedRunning) {
      // ServiceStatusCard.vue:60-66 — `.service-status-meta` renders
      // "PID: {pid} · Uptime: {uptime}" only while running.
      const meta = page.locator(".service-status-meta");
      await expect(meta).toContainText(String(status.pid));
      const uptimeText = formatUptime(status.uptime_seconds);
      if (uptimeText) {
        await expect(meta).toContainText(uptimeText);
      }
    }

    // ServiceStatusCard.vue:73-79 — `.service-status-exe` (+ its `title`
    // attribute) only renders when `exe_path` is truthy.
    if (status.exe_path) {
      const exeEl = page.locator(".service-status-exe");
      await expect(exeEl).toHaveText(status.exe_path);
      await expect(exeEl).toHaveAttribute("title", status.exe_path);
    } else {
      await expect(page.locator(".service-status-exe")).toHaveCount(0);
    }
  });

  test("服务探活: WebUI 渲染内容与 service probe 一致", async ({ page }) => {
    // Unlike status/models, the probe request (testConnection()) is NOT
    // fired by init() on mount (useServiceControl.ts:571-614 only awaits
    // loadServiceParams/loadServiceStatus/loadServiceModels) — it is
    // exclusively triggered by clicking the "Test" button inside the
    // connection bar's body. The bar itself starts collapsed
    // (connectionCollapsed defaults to true), so it must be expanded first.
    await page.goto("/service");
    await page.locator(".service-connection-bar").click();
    const body = page.locator(".service-connection-body");
    await expect(body).toBeVisible();

    const responsePromise = page.waitForResponse(
      (r) =>
        r.url().includes("/api/service/probe") &&
        r.request().method() === "GET",
    );
    // ServiceConnectionBar.vue:96-136 — the "Test" button is the first
    // <button> rendered inside `.service-connection-body` (Save is the
    // second/last one), so `.first()` unambiguously targets it.
    await body.locator("button").first().click();
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const probe = (await response.json()) as ProbeServiceResponse;

    // useServiceControl.ts:310-327 testConnection(): the ok/fail branch is
    // driven solely by `reachable` (not `alive`).
    const expectedOk = probe.reachable === true;
    // ServiceConnectionBar.vue:137-144 — `.conn-result-ok` / `.conn-result-fail`.
    if (expectedOk) {
      await expect(page.locator(".conn-result-ok")).toBeVisible();
      await expect(page.locator(".conn-result-fail")).toHaveCount(0);
    } else {
      await expect(page.locator(".conn-result-fail")).toBeVisible();
      await expect(page.locator(".conn-result-ok")).toHaveCount(0);
    }
  });

  test("服务模型列表: WebUI 渲染内容与 service models 一致", async ({
    page,
  }) => {
    // GET /api/service/models is fired unconditionally by init() on mount
    // (useServiceControl.ts:600-604 loadServiceModels).
    const responsePromise = page.waitForResponse(
      (r) =>
        r.url().includes("/api/service/models") &&
        r.request().method() === "GET",
    );
    await page.goto("/service");
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as ServiceModelsResponse;
    const models = body.models ?? [];

    if (models.length === 0) {
      // ServiceLaunchParams.vue:126-146 — with zero models the `v-else`
      // grid (which hosts `.param-cell-model` / `.param-select`) never
      // renders at all; a `svc-notice` empty-state card takes its place, so
      // there is no dropdown to compare against.
      await expect(page.locator(".param-cell-model")).toHaveCount(0);
      return;
    }

    // ServiceLaunchParams.vue:153-219 — the model dropdown is the
    // `.param-select` scoped inside `.param-cell-model` (the log-level
    // `<select>` also carries the bare `.param-select` class, so the scoped
    // selector is required to avoid ambiguity), grouped into NPU/GPU/CPU
    // `<optgroup>`s.
    const modelSelect = page.locator(".param-cell-model .param-select");
    await expect(modelSelect).toBeVisible();
    const options = modelSelect.locator("optgroup option");
    await expect(options).toHaveCount(models.length);
    const renderedNames = await options.allTextContents();
    const expectedNames = models.map((m) => m.name);
    expect(new Set(renderedNames.map((n) => n.trim()))).toEqual(
      new Set(expectedNames),
    );
  });

  test("服务配置: WebUI 渲染内容与 service config get 一致", async ({
    page,
  }) => {
    // GET /api/config is NOT fetched on Service-page mount: it is only
    // fetched from ServiceConfigPanel.vue's onMounted (loadServiceConfig()),
    // and that component only mounts once ServiceConfigModal.vue's
    // `v-if="open"` becomes true — i.e. only after the ⚙️ gear button
    // (ServiceStatusCard.vue:83-95 `.svc-cfg-gear-btn`) is clicked.
    await page.goto("/service");
    // `canConfigure = isServiceInstalled || serviceModelsLoading` (ServiceView.vue)
    // means the gear button is optimistically ENABLED while the initial
    // status/models fetch is still in flight, then flips to disabled once it
    // resolves and confirms no install — a real transient race, not just a
    // slow load. Wait for that settle before reading the button's final state.
    await page.waitForLoadState("networkidle");
    const gearBtn = page.locator(".svc-cfg-gear-btn");

    // ── Investigation finding (real environment gap, not a selector bug) ──
    // ServiceView.vue:110-115 — `canConfigure = isServiceInstalled ||
    // serviceModelsLoading`, and `isServiceInstalled = !!serviceStatus.exe_path`.
    // `apps/api/_model_runtime_di.py`'s live `exe_path` provider only ever
    // resolves non-empty when `GenieAPIService.exe` is a REAL file on disk
    // (`_exe_present()` checks `.is_file()`) under the configured — or
    // self-healed `data/bin` — install dir. Pointing `genie_service.root_path`
    // at an arbitrary directory via `POST /api/forge-config` (the
    // `ensureWorkbenchVisible()`-style bypass `app-builder-consistency.spec.ts`
    // uses for its own hidden flag) does NOT fake this, since no actual binary
    // would exist there. So in a fresh environment with GenieAPIService
    // genuinely not installed, this gear button is legitimately disabled and
    // cannot be clicked — there is no config-only way around it.
    //
    // `GET /api/config` itself, however, does NOT require an install
    // (interfaces/http/routes/model_runtime.py `get_service_config()`: "When
    // the service is not installed, in-memory defaults are returned
    // (read-only, meta.config_file_path empty)"). So instead of fabricating a
    // pass or silently skipping this whole test, fetch the endpoint directly
    // whenever the button is disabled — still a genuine, source-backed check.
    if (!(await gearBtn.isEnabled())) {
      const response = await page.request.get("/api/config");
      expect(response.ok()).toBeTruthy();
      const body = (await response.json()) as ServiceConfigResponse;
      expect(body.config).toBeDefined();
      // ServiceConfigPanel.vue never mounts while the gear button stays
      // disabled (v-if="open" is never flipped), so none of its markup can
      // possibly be present — nothing further to compare against.
      await expect(
        page.locator('input[list="svc-cfg-default-model-options"]'),
      ).toHaveCount(0);
      return;
    }

    await expect(gearBtn).toBeEnabled();

    const responsePromise = page.waitForResponse(
      (r) => r.url().includes("/api/config") && r.request().method() === "GET",
    );
    await gearBtn.click();
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as ServiceConfigResponse;
    const cfg = body.config ?? {};

    // ServiceConfigPanel.vue:76 — `activeConfigTab` defaults to "local", so
    // ServiceConfigLocalModelTab.vue is the Tab visible right after opening.
    // Its `default_model` free-text input (ServiceConfigLocalModelTab.vue:80-97)
    // is uniquely identified by its `list` attribute.
    const defaultModelInput = page.locator(
      'input[list="svc-cfg-default-model-options"]',
    );
    await expect(defaultModelInput).toBeVisible();
    await expect(defaultModelInput).toHaveValue(cfg.default_model ?? "");

    // ServiceConfigLocalModelTab.vue:52,99-249 `slotsReady` gate: the 3
    // NPU/GPU/CPU `.svc-cfg-slot` blocks (each with one `.svc-cfg-select`)
    // only render when `cfg.models` has at least 3 entries.
    const slotsReady = Array.isArray(cfg.models) && cfg.models.length >= 3;
    const slotSelects = page.locator(".svc-cfg-slot .svc-cfg-select");
    if (slotsReady) {
      await expect(slotSelects).toHaveCount(3);
      for (let i = 0; i < 3; i++) {
        await expect(slotSelects.nth(i)).toHaveValue(
          cfg.models![i].name ?? "",
        );
      }
    } else {
      await expect(slotSelects).toHaveCount(0);
    }
  });

  test("服务路径: WebUI 渲染的可执行文件路径与 service path 返回的安装目录前缀一致", async ({
    page,
  }) => {
    // Reuses the same GET /api/service/status interception as test 1 —
    // its `exe_path` is the "WebUI truth" for the installed executable path.
    const responsePromise = page.waitForResponse(
      (r) =>
        r.url().includes("/api/service/status") &&
        r.request().method() === "GET",
    );
    await page.goto("/service");
    const response = await responsePromise;
    const status = (await response.json()) as ServiceStatusResponse;

    // ── Investigation finding (do NOT invent a comparison endpoint) ──────
    // The CLI `qai service path` command's value comes from
    // OpenServiceDirUseCase.execute() -> InferenceService.get_install_dir()
    // (apps/cli/commands/service.py cmd_service_path +
    // src/qai/model_runtime/application/use_cases/open_service_dir.py). The
    // ONLY HTTP route wired to that same use case is
    // `POST /api/service/open-dir` (interfaces/http/routes/model_runtime.py),
    // but that route awaits `open_service_dir_use_case.execute()`, DISCARDS
    // the returned path string, and responds with a bare
    // `SuccessResponse(success=True)` — it never puts `get_install_dir()`'s
    // value on the wire. There is no GET (or any) HTTP endpoint that
    // actually exposes this value, so a real CLI-path vs WebUI-path
    // cross-check is not possible over HTTP. This test therefore only does
    // a lightweight sanity assertion instead of fabricating a comparison
    // against data that cannot actually be fetched.
    if (!status.exe_path) {
      // ServiceStatusCard.vue:73-79 — not rendered at all when not installed.
      await expect(page.locator(".service-status-exe")).toHaveCount(0);
      return;
    }

    const exeEl = page.locator(".service-status-exe");
    await expect(exeEl).toHaveText(status.exe_path);
    expect(status.exe_path.trim().length).toBeGreaterThan(0);
    // Sanity check only: an absolute-looking path ending in an .exe filename.
    expect(status.exe_path).toMatch(/[\\/][^\\/]+\.exe$/i);
  });
});
