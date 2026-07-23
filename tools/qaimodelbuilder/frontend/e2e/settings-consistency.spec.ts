import { test, expect } from "@playwright/test";

/**
 * Settings page (Cloud Models tab) — WebUI / HTTP API consistency.
 *
 * See .junie/plans/qai-webui-e2e-consistency-test.md for the full design.
 * `SettingsView.vue` lazily mounts `CloudModelsPanel.vue` on first visit to
 * the `cloud-models` tab (see the `visited` gate in `SettingsView.vue`), and
 * `CloudModelsPanel.vue` unconditionally fetches
 * `GET /api/model-catalog/providers` in its `onMounted` hook. Navigating
 * directly to `/settings?tab=cloud-models` is the reliable way to trigger
 * that mount + fetch (confirmed: `SettingsView.vue` seeds its `?tab=` query
 * value as both the initial `activeTab` and the initial `visited` entry),
 * rather than clicking the tab button after the page has already settled.
 *
 * Note: `ui.theme` is deliberately NOT covered by this file — it is a pure
 * client-side localStorage preference with no backend field and no
 * rendering on this page, so there is no valid API/UI pair to compare.
 */

interface CloudModel {
  model_id: string;
  name?: string;
  context_length?: number;
  description?: string;
  supports_streaming?: boolean;
  api_model_id?: string;
  params?: Record<string, unknown>;
  [key: string]: unknown;
}

interface ProviderConfig {
  base_url?: string;
  pinned?: boolean;
  models?: CloudModel[];
  [key: string]: unknown;
}

interface ProviderRow {
  provider_id: string;
  config: ProviderConfig;
}

interface ProvidersResponse {
  providers: ProviderRow[];
}

test.describe("Settings — Cloud Models 一致性", () => {
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

  test("Provider 列表: WebUI 渲染内容与 config provider list 一致", async ({
    page,
  }) => {
    // UI 人工验证路径：直接导航到 /settings?tab=cloud-models（比先打开
    // /settings 再点击 "☁️ Cloud Models" 标签更可靠 —— SettingsView.vue 用
    // `?tab=` 查询参数预置初始 activeTab + visited 集合，直达 URL 能确保
    // CloudModelsPanel.vue 在本次导航中首次挂载并触发 onMounted 的
    // fetchProviders() 请求）。
    const responsePromise = page.waitForResponse(
      (r) =>
        r.url().includes("/api/model-catalog/providers") &&
        r.request().method() === "GET",
    );
    await page.goto("/settings?tab=cloud-models");
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as ProvidersResponse;
    const providers = body.providers ?? [];

    // filteredProviders 在搜索框为空时直接返回 providers.value 本身（顺序不变），
    // 因此 .cloud-model-provider-group 的渲染顺序与 API 返回顺序完全一致，可按
    // 下标逐一核对，不需要额外按 provider_id 反查分组。
    const groups = page.locator(".cloud-model-provider-group");
    await expect(groups).toHaveCount(providers.length);

    for (let i = 0; i < providers.length; i++) {
      const row = providers[i];
      const group = groups.nth(i);

      await expect(group.locator(".cloud-model-provider-label")).toContainText(
        row.provider_id,
      );

      const models = row.config.models ?? [];
      const cards = group.locator(".cloud-model-card");
      await expect(cards).toHaveCount(models.length);

      const renderedNames = await cards
        .locator(".cloud-model-card-name")
        .allTextContents();
      const expectedNames = models.map((m) => (m.name ?? m.model_id).trim());
      expect(renderedNames.map((n) => n.trim())).toEqual(expectedNames);
    }
  });
});
