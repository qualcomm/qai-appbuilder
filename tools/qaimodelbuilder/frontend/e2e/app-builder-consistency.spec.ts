import { test, expect, type Page } from "@playwright/test";

/**
 * App Builder workbench — CLI / HTTP API / WebUI consistency.
 *
 * See .junie/plans/qai-webui-e2e-consistency-test.md for the full design.
 * Module C of test/test_builder_cli.py already verified `qai pack list` /
 * `qai run list` (CLI) against `GET /api/app-builder/models` /
 * `.../runs` (HTTP API) — field-for-field equal except a known gap (D0003:
 * the API exposes 6 extra fields the CLI omits). This file closes the
 * remaining leg of the "same kernel, every skin" chain: does the
 * browser-rendered WebUI show exactly what that same HTTP API returned?
 * CLI==API (already established) + API==UI (this file) together
 * demonstrate CLI==UI transitively, without re-driving the CLI here.
 */

interface AppBuilderModel {
  id: string;
  title: string;
  taxonomy: string[];
  [key: string]: unknown;
}

interface AppBuilderRun {
  id: string | null;
  [key: string]: unknown;
}

async function ensureCsrfToken(page: Page): Promise<string | undefined> {
  const existing = (await page.context().cookies()).find(
    (c) => c.name === "qai_csrf",
  );
  if (existing) return existing.value;
  await page.request.get("/api/system/health");
  return (await page.context().cookies()).find((c) => c.name === "qai_csrf")
    ?.value;
}

/**
 * Force `ui.app_builder.show_workbench = true` so the App Builder overlay
 * actually mounts on click — it is hidden by default (see
 * frontend/src/composables/useForgeConfig.ts). Mirrors what a human would
 * do once via Settings -> App Config, but through the same officially
 * supported `/api/forge-config` surface the Settings dialog itself uses,
 * so this suite doesn't need to drive that dialog for every run.
 */
async function ensureWorkbenchVisible(page: Page): Promise<void> {
  const token = await ensureCsrfToken(page);
  const res = await page.request.get("/api/forge-config");
  const body = (await res.json()) as { config?: Record<string, unknown> };
  const config = body.config ?? {};
  const ui = (config.ui as Record<string, unknown>) ?? {};
  const appBuilder = (ui.app_builder as Record<string, unknown>) ?? {};
  const nextConfig = {
    ...config,
    ui: { ...ui, app_builder: { ...appBuilder, show_workbench: true } },
  };
  await page.request.post("/api/forge-config", {
    headers: token ? { "X-QAI-CSRF": token } : {},
    data: { config: nextConfig },
  });
}

async function openAppBuilderWorkbench(page: Page): Promise<void> {
  await page.goto("/chat");
  await page.getByTestId("mode-btn-app-builder").click();
  await expect(page.getByTestId("app-builder-workbench")).toBeVisible();
}

/**
 * Force the persisted CC SDK opt-in flag (`ai_coding.config.enabled`) to
 * `true` via `POST /api/cc/config` so `cc-pill` actually renders in
 * ChatComposer.vue — it is HIDDEN by default on a fresh install (verified in
 * `qai/user_prefs/application/use_cases/forge_config.py`'s
 * `_read_coding_enabled()`: defaults to `False` when the `ai_coding.config`
 * KV document is empty, which then flows into the frontend's `ccEnabled`
 * computed via `GET /api/forge-config`'s DERIVED `ai_coding.cc.enabled`
 * field). Without this, `page.getByTestId("cc-pill")` never appears in the
 * DOM and any click on it times out.
 *
 * NOTE: unlike `ensureWorkbenchVisible()`, this does NOT go through
 * `POST /api/forge-config` — `ai_coding.cc.enabled` is re-derived from the
 * `ai_coding.config` document on every forge-config load, so posting it
 * directly there would be silently overwritten on the very next read. The
 * real, officially supported write path (the same one the Settings -> App
 * Config CC toggle uses) is `POST /api/cc/config`.
 */
async function ensureCcPillVisible(page: Page): Promise<void> {
  const token = await ensureCsrfToken(page);
  await page.request.post("/api/cc/config", {
    headers: token ? { "X-QAI-CSRF": token } : {},
    data: { config: { enabled: true } },
  });
}

test.describe("App Builder workbench — CLI/API/UI consistency", () => {
  test.beforeEach(async ({ page }) => {
    await ensureWorkbenchVisible(page);
  });

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

  test("Pack 列表: WebUI 渲染内容与 GET /api/app-builder/models 完全一致（按任务分区校验）", async ({
    page,
  }) => {
    // UI 人工验证路径：打开 /chat 页面 -> 点击工具栏 "App Builder" 图标
    // （data-testid="mode-btn-app-builder"）-> 等待工作台面板弹出
    // （data-testid="app-builder-workbench"）。实测确认（见
    // AppBuilderWorkbenchOverlay.vue 的 wb.cardsForSelection，按
    // store.selectedTaskId 过滤 store.models）：工作台一旦加载完模型列表就会
    // 自动选中一个模型，且无论顶部模型选择器弹出层（.ab-model-picker-button ->
    // .ab-model-picker-popover）还是未选中模型时的画廊态，展示的都不是全部
    // 模型，而是被当前激活的任务分类过滤后的子集——没有任何 UI 入口能一次性看到
    // 扁平未过滤的全量列表。因此本测试改为：先把 API 返回的每个模型按其
    // taxonomy 任务段分组，再依次通过顶部任务选择器（TaxonomyPickerDropdown，
    // 点击 .ab-taxonomy-btn 打开弹出层 -> 用弹出层内搜索框按任务 id 过滤后精确
    // 定位对应的 .ab-taxonomy-task-row 并点击选中）切到每个任务，逐个打开模型
    // 选择器弹出层核对该任务下渲染的模型名称集合与 API 对应子集完全一致；累计
    // 遍历完所有任务后即等价于验证了 API 返回的全部模型，只是按 UI 真实的
    // 任务分区交互方式校验，而不是假设存在一个扁平未过滤的列表。
    const responsePromise = page.waitForResponse(
      (r) =>
        r.url().includes("/api/app-builder/models") &&
        r.request().method() === "GET",
    );
    await openAppBuilderWorkbench(page);
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as { items: AppBuilderModel[] };
    const apiItems = body.items;

    // taxonomy 是后端 manifest_taxonomy_segments() 产出的 (group, task) 段
    // 元组；与 useAppBuilderWorkbench.ts 的 taskGroups 回退逻辑
    // （path[1] ?? groupId）以及 cardsForSelection 的
    // model.taxonomy.includes(selectedTaskId) 匹配方式保持一致：任务段取
    // taxonomy[1]，只有单段（只有 group、没有 task）时才退化为用 taxonomy[0]
    // 本身当任务 id。
    function taskKeyOf(item: AppBuilderModel): string {
      const taxonomy = item.taxonomy ?? [];
      return taxonomy[1] ?? taxonomy[0] ?? "";
    }

    const byTask = new Map<string, AppBuilderModel[]>();
    for (const item of apiItems) {
      const key = taskKeyOf(item);
      const bucket = byTask.get(key);
      if (bucket) bucket.push(item);
      else byTask.set(key, [item]);
    }

    // 任务 id -> 人类可读 label，与 TaxonomyPickerDropdown 渲染的任务行文本
    // （t("appBuilder.taxonomy.task." + id, label) 在缺失 i18n key 时回退到
    // label）对齐，用于在弹出层里精确定位任务行，避免任务 id 互为子串
    // （如 "vision" / "computer-vision"）导致的误匹配。
    const treeRes = await page.request.get("/api/app-builder/taxonomy/tree");
    expect(treeRes.ok()).toBeTruthy();
    const tree = (await treeRes.json()) as {
      groups: Array<{ tasks: Array<{ id: string; label: string }> }>;
    };
    const labelByTaskId = new Map<string, string>();
    for (const g of tree.groups) {
      for (const t of g.tasks) labelByTaskId.set(t.id, t.label);
    }
    function labelOf(taskId: string): string {
      const known = labelByTaskId.get(taskId);
      if (known) return known;
      return taskId
        .split(/[-_]/)
        .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
        .join(" ");
    }

    const coveredIds = new Set<string>();
    for (const [taskId, models] of byTask) {
      const label = labelOf(taskId);

      await page.locator(".ab-taxonomy-btn").click();
      const taxonomyPopover = page.locator(".ab-taxonomy-popover--floating");
      await expect(taxonomyPopover).toBeVisible();
      await taxonomyPopover.locator(".ab-taxonomy-search input").fill(taskId);

      const taskRows = taxonomyPopover.locator(".ab-taxonomy-task-row");
      await expect(taskRows.first()).toBeVisible();
      const rowCount = await taskRows.count();
      let targetRow = null;
      for (let i = 0; i < rowCount; i++) {
        const text = await taskRows.nth(i).locator("b").innerText();
        if (text === label) {
          targetRow = taskRows.nth(i);
          break;
        }
      }
      expect(
        targetRow,
        `任务 "${taskId}" (${label}) 应能在任务选择器中被精确定位到`,
      ).not.toBeNull();
      await targetRow!.click();
      await expect(taxonomyPopover).toBeHidden();

      await page.locator(".ab-model-picker-button").click();
      const modelPopover = page.locator(".ab-model-picker-popover");
      await expect(modelPopover).toBeVisible();
      const cards = modelPopover.locator(".ab-model-card");
      await expect(cards).toHaveCount(models.length);

      const renderedNames = await cards
        .locator(".ab-model-card-name")
        .allTextContents();
      const expectedNames = models.map((m) => m.title || m.id);
      expect(new Set(renderedNames)).toEqual(new Set(expectedNames));

      for (const m of models) coveredIds.add(m.id);
      await page.keyboard.press("Escape");
      await expect(modelPopover).toBeHidden();
    }

    expect(coveredIds).toEqual(new Set(apiItems.map((m) => m.id)));
  });

  test("Run 列表: WebUI 渲染条目数与 GET /api/app-builder/runs 一致", async ({
    page,
  }) => {
    // UI 人工验证路径：在已打开的工作台面板里先打开顶部模型选择器
    // （.ab-model-picker-button）点击一个模型条目（选中模型触发一次运行历史拉取
    // -- store.selectModel() 内部无条件调用 loadModelHistoryAndRestore()，即使
    // 点击的是当前已自动选中的同一个模型也会重新拉取 -- 历史面板打开本身只是展示
    // 已拉取到的数据、不会再发一次请求，故必须在点击历史图标之前先点一次模型条目
    // 才能等到这次 GET）-> 再点击右上角的历史图标（data-testid=
    // "app-builder-history-toggle"）-> 面板展开后查看运行记录条数（或"暂无记录"
    // 的空态提示），与命令行 `qai run list` 的输出条数比对（该 CLI 输出已由
    // test_builder_cli.py 模块 C 验证过与本测试拦截到的同一个 HTTP 响应体一致）。
    await openAppBuilderWorkbench(page);
    const responsePromise = page.waitForResponse(
      (r) =>
        r.url().includes("/api/app-builder/runs") &&
        r.request().method() === "GET",
    );
    await page.locator(".ab-model-picker-button").click();
    await page.locator(".ab-model-picker-popover .ab-model-card").first().click();
    const historyToggle = page.getByTestId("app-builder-history-toggle");
    await expect(historyToggle).toBeEnabled();
    await historyToggle.click();

    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as { runs: AppBuilderRun[] };
    const apiRuns = body.runs;

    await expect(page.getByTestId("app-builder-history-panel")).toBeVisible();
    if (apiRuns.length === 0) {
      await expect(page.locator(".ab-history-empty")).toBeVisible();
    } else {
      await expect(page.getByTestId("app-builder-history-item")).toHaveCount(
        apiRuns.length,
      );
    }
  });

  test("最近对话: WebUI 侧边栏渲染内容与 conv list 一致", async ({ page }) => {
    // AppSidebar.vue 的 "Recent Chats" 区块是常驻在每个页面的全局侧边栏
    // （App.vue 顶层挂载 <AppSidebar />，不在任何路由 <RouterView> 之内），其
    // onMounted 无条件调用 conversationsStore.fetch() -> GET
    // /api/chat/conversations（不要求处于 /chat、也不要求已有会话——为空时渲染
    // .conv-empty-hint 空态提示）。分组列表本身没有 data-testid，条目用
    // .conv-item（标题文本在其内的 .conv-item-title-text），按
    // useConversationGrouping.ts 的分组规则渲染：置顶会话（pinned）单独一组且
    // 不裁剪；其余按 updated_at 落入 today/yesterday/thisWeek/thisMonth/earlier
    // 五个时间桶后，初始（未展开）状态下每桶只渲染前 CONV_GROUP_CAP=5 条。
    interface ConversationSummary {
      id: string;
      title: string;
      updated_at: string;
      pinned?: boolean;
    }
    const responsePromise = page.waitForResponse(
      (r) =>
        r.url().includes("/api/chat/conversations") &&
        r.request().method() === "GET",
    );
    await page.goto("/chat");
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as { items: ConversationSummary[] };
    const apiItems = body.items;

    if (apiItems.length === 0) {
      await expect(page.locator(".conv-empty-hint")).toBeVisible();
      return;
    }

    const CONV_GROUP_CAP = 5;
    function groupKeyOf(iso: string): string {
      const ts = Date.parse(iso);
      if (!Number.isFinite(ts)) return "earlier";
      const now = new Date();
      const todayStart = new Date(
        now.getFullYear(),
        now.getMonth(),
        now.getDate(),
      ).getTime();
      const yesterdayStart = todayStart - 86400000;
      const weekStart = todayStart - 6 * 86400000;
      const monthStart = todayStart - 29 * 86400000;
      if (ts >= todayStart) return "today";
      if (ts >= yesterdayStart) return "yesterday";
      if (ts >= weekStart) return "thisWeek";
      if (ts >= monthStart) return "thisMonth";
      return "earlier";
    }

    const pinned = apiItems.filter((c) => c.pinned === true);
    const unpinned = apiItems.filter((c) => c.pinned !== true);
    const byBucket = new Map<string, ConversationSummary[]>();
    for (const c of unpinned) {
      const key = groupKeyOf(c.updated_at);
      const bucket = byBucket.get(key);
      if (bucket) bucket.push(c);
      else byBucket.set(key, [c]);
    }
    const expectedItems: ConversationSummary[] = [...pinned];
    for (const bucket of byBucket.values()) {
      expectedItems.push(...bucket.slice(0, CONV_GROUP_CAP));
    }

    const rows = page.locator(".conv-item");
    await expect(rows.first()).toBeVisible();
    await expect(rows).toHaveCount(expectedItems.length);
    const renderedTitles = await rows
      .locator(".conv-item-title-text")
      .allTextContents();
    // 空标题会回退渲染 i18n 的 "Untitled" 文案（与 API 的空字符串不相等），故只
    // 比对非空标题，避免与本测试无关的 i18n 文案耦合。
    const expectedTitles = expectedItems
      .map((c) => c.title)
      .filter((t) => t !== "");
    expect(new Set(renderedTitles.filter((t) => t !== ""))).toEqual(
      new Set(expectedTitles),
    );
  });

  test("Pack 任务分类: WebUI 任务选择器渲染内容与 pack taxonomy 一致", async ({
    page,
  }) => {
    // TaxonomyPickerDropdown.vue 收到的 :taxonomy="taxonomyForPicker"
    // （AppBuilderWorkbenchOverlay.vue）来自 useAppBuilderWorkbench.ts 的
    // taskGroups：当 store.taxonomyTree（fetchTaxonomyTree() 拉取的 GET
    // /api/app-builder/taxonomy/tree 原始响应）非空时逐字段透传
    // id/label/tasks，因此弹出层左侧的分组行、以及点击底部 "View all" 链接后
    // 展平出的全量任务行，都应与该接口响应一一对应。
    interface TaxonomyTreeResponse {
      groups: Array<{
        id: string;
        label: string;
        tasks: Array<{ id: string; label: string }>;
      }>;
    }
    const treeResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes("/api/app-builder/taxonomy/tree") &&
        r.request().method() === "GET",
    );
    await openAppBuilderWorkbench(page);
    const treeResponse = await treeResponsePromise;
    expect(treeResponse.ok()).toBeTruthy();
    const tree = (await treeResponse.json()) as TaxonomyTreeResponse;

    await page.locator(".ab-taxonomy-btn").click();
    const taxonomyPopover = page.locator(".ab-taxonomy-popover--floating");
    await expect(taxonomyPopover).toBeVisible();

    // 左侧分组列表始终展示全部分组，不受当前浏览分组过滤。
    const groupRows = taxonomyPopover.locator(".ab-taxonomy-group-row");
    await expect(groupRows).toHaveCount(tree.groups.length);
    const renderedGroupLabels = await groupRows
      .locator(".ab-taxonomy-group-label")
      .allTextContents();
    expect(new Set(renderedGroupLabels)).toEqual(
      new Set(tree.groups.map((g) => g.label)),
    );

    // 右侧任务列表默认只显示当前浏览分组下的任务；点击底部 "View all N tasks →"
    // 链接（.ab-taxonomy-foot a，仅在未点击过时渲染）切到 showAll=true 后的展平
    // 全量任务列表，才能一次性核对全部任务。
    await taxonomyPopover.locator(".ab-taxonomy-foot a").click();
    const totalTaskCount = tree.groups.reduce((n, g) => n + g.tasks.length, 0);
    const taskRows = taxonomyPopover.locator(".ab-taxonomy-task-row");
    await expect(taskRows).toHaveCount(totalTaskCount);
    const renderedTaskNames = await taskRows.locator("b").allTextContents();
    const expectedTaskNames = tree.groups.flatMap((g) =>
      g.tasks.map((t) => t.label),
    );
    expect(new Set(renderedTaskNames)).toEqual(new Set(expectedTaskNames));
  });

  test("AI 编程会话列表: WebUI 渲染内容与 code session list 一致", async ({
    page,
  }) => {
    // cc-pill（Claude Code 模式）点击后触发 useSessionCrud.ts 的
    // enterMode() -> fetchSessions()（GET /api/cc/sessions），随后
    // ChatView.vue 因 claudeCode.isCCMode 变为 true 切换渲染
    // ChatViewClaudeCode.vue，挂载 data-testid="coding-panel-cc" 面板。读
    // CodingSessionPanel.vue 模板确认：其会话列表容器
    // data-testid="coding-panel-list-cc" 下的每一行都是直接子 <div>（模板用
    // <template v-for> 包裹，不产生额外包裹节点），行内没有独立的
    // data-testid / class（纯内联样式），行内第一个 <span> 是状态图标、第二个
    // <span> 才是标题文本（{{ s.title ?? s.session_id }}）。"Active Sessions"
    // 标签页（默认激活）只渲染 status === "active" 的会话——其余状态的会话只出
    // 现在 History 标签页（来自另一个独立的 /sessions/history/all 接口），故
    // 按同样规则过滤 API 响应后再逐行按序比对。
    //
    // 前置条件：读 useForgeConfig.ts 的 ccEnabled/forge_config.py 的
    // _read_coding_enabled() 确认 cc-pill 的 v-if="ccEnabled" 在全新后端上
    // 实际默认是 false（ai_coding.config KV 文档为空时的防御性回退，而不是
    // useForgeConfig.ts 注释所声称的"后端默认 true"）——真正的出厂 True 默认
    // 只存在于 ai_coding 子上下文自身的 CodingSessionConfig 领域层，从未被
    // /api/forge-config 的派生逻辑读取。因此 pill 在全新安装上根本不会挂载到
    // DOM，点击会一直等到超时；须先用 ensureCcPillVisible() 通过官方配置写入
    // 接口打开该开关。
    interface CodingSessionSummary {
      session_id: string;
      title: string | null;
      status: string;
    }
    await ensureCcPillVisible(page);
    const responsePromise = page.waitForResponse(
      (r) =>
        r.url().includes("/api/cc/sessions") && r.request().method() === "GET",
    );
    await page.goto("/chat");
    await page.getByTestId("cc-pill").click();
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as {
      sessions: CodingSessionSummary[];
    };
    const activeApiSessions = body.sessions.filter(
      (s) => s.status === "active",
    );

    await expect(page.getByTestId("coding-panel-cc")).toBeVisible();
    const rows = page.getByTestId("coding-panel-list-cc").locator("> div");
    if (activeApiSessions.length === 0) {
      await expect(rows).toHaveCount(1);
      return;
    }
    await expect(rows).toHaveCount(activeApiSessions.length);
    for (let i = 0; i < activeApiSessions.length; i++) {
      const expected = activeApiSessions[i]!;
      await expect(rows.nth(i).locator("span").nth(1)).toHaveText(
        expected.title ?? expected.session_id,
      );
    }
  });

  test("AI 编程环境健康检查: WebUI 渲染内容与 code health 一致", async ({
    page,
  }) => {
    // CodingSessionPanel.vue 挂载后 onMounted 调用 loadHealth() ->
    // fetchCcHealth()（GET /api/cc/health）。读源码确认：
    // data-testid="coding-panel-hint-cc" 仅在
    // activeTab === 'active' && !envReady && !healthLoading 时渲染
    // （envReady = isSdkAvailable && isAuthConfigured，kind==='cc' 时两者分别
    // 直接取自响应的 sdk_available / auth_configured 字段），因此用该布尔组合
    // 断言 hint 的可见性即可，不去匹配 hint 内部会随 i18n 变化的文案。页脚状态条
    // （kind==='cc' 分支）另外渲染了不受 i18n 影响的固定 "✅"/"❌" 字符（分别对应
    // SDK 状态、Auth 状态）以及原样输出的 sdk_version 文本，这三处是可确认的渲染
    // 内容，一并核对。响应中还有 models 字段，但读 CodingSessionPanel.vue 全文
    // 确认模板并未用它渲染任何内容，因此不为它构造断言。
    //
    // 前置条件：同上一测试，cc-pill 的 v-if="ccEnabled" 在全新后端上默认为
    // false，须先用 ensureCcPillVisible() 打开该开关，否则 pill 根本不会挂载
    // 到 DOM，点击会一直等到超时。
    interface CcHealthResponse {
      sdk_available?: boolean;
      auth_configured?: boolean;
      sdk_version?: string;
    }
    await ensureCcPillVisible(page);
    const healthResponsePromise = page.waitForResponse(
      (r) =>
        r.url().includes("/api/cc/health") && r.request().method() === "GET",
    );
    await page.goto("/chat");
    await page.getByTestId("cc-pill").click();
    const healthResponse = await healthResponsePromise;
    expect(healthResponse.ok()).toBeTruthy();
    const health = (await healthResponse.json()) as CcHealthResponse;
    const sdkAvailable = health.sdk_available === true;
    const authConfigured = health.auth_configured === true;
    const envReady = sdkAvailable && authConfigured;

    await expect(page.getByTestId("coding-panel-cc")).toBeVisible();
    const hint = page.getByTestId("coding-panel-hint-cc");
    if (envReady) {
      await expect(hint).toBeHidden();
    } else {
      await expect(hint).toBeVisible();
    }

    // 页脚状态条 kind==='cc' 分支固定先渲染 SDK 状态 span 再渲染 Auth 状态 span
    // （见 CodingSessionPanel.vue template `v-if="kind === 'cc'"` 分支）。
    const infoSpans = page.locator(".ai-coding-statusbar-info > span");
    await expect(infoSpans.nth(0)).toContainText(sdkAvailable ? "✅" : "❌");
    await expect(
      infoSpans.nth(0).locator(".ai-coding-statusbar-detail"),
    ).toHaveText(health.sdk_version ?? "");
    await expect(infoSpans.nth(1)).toContainText(authConfigured ? "✅" : "❌");
  });
});
