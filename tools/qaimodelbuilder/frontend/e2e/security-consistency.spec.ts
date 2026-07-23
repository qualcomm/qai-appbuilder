import { test, expect } from "@playwright/test";

/**
 * Security page — CLI / HTTP API / WebUI consistency.
 *
 * See .junie/plans/qai-webui-e2e-consistency-test.md for the full design.
 * This file is part of the QAIModelBuilder WebUI consistency suite:
 * CLI==API (already verified by test/test_builder_cli.py) + API==UI (this
 * file) => CLI==UI transitively. Do NOT re-drive the CLI here — the CLI leg
 * is already established elsewhere; this file only closes the API==UI leg
 * for the `/security` page's tabs.
 *
 * `/security` has no precondition/config flag — it is visible by default
 * (unlike the App Builder workbench, see app-builder-consistency.spec.ts),
 * so unlike that file this suite needs no `ensureCsrfToken` /
 * `ensureWorkbenchVisible`-style bypass and issues no mutating requests at
 * all. It also has very few `data-testid`s — most panels here are
 * CSS-class-only (confirmed by reading each panel's source directly; see
 * the per-test comments below for the concrete selectors). Every test
 * below registers `page.waitForResponse(...)` for the exact GET the
 * triggering click causes *before* clicking, then asserts the rendered DOM
 * against that same response body.
 *
 * Skill `globalMode` (`GET /api/skills/policy`) is intentionally NOT
 * covered by skills-consistency.spec.ts — its real UI counterpart lives in
 * `SkillCapabilitiesPanel.vue` on this page (see test 6 below).
 */

// ── Wire-format types (mirror the confirmed backend DTOs / composables) ────

interface PolicyRuleDTO {
  rule_id: string;
  scope: "user" | "preset" | "path";
  pattern: string;
  case_sensitive: boolean;
  action: "allow" | "deny";
  description: string;
  op: "read" | "write" | "exec" | "exec_deny" | "any";
}

interface PolicyResponse {
  version: number;
  updated_at: string;
  rules: PolicyRuleDTO[];
  enabled: boolean;
  mode: "enforce" | "audit_only";
  dynamic_authorization: boolean;
  no_ui_channels: string[];
  needs_reboot: boolean;
}

interface DiscoveredSkillEntry {
  skill_name: string;
  capability_name: string;
  [key: string]: unknown;
}

interface SkillDiscoveryResponse {
  skills: DiscoveredSkillEntry[];
  total: number;
  scan_status: string;
  by_name?: Record<string, DiscoveredSkillEntry>;
}

/** Subset of `RuntimeConfig` (useRuntimeConfig.ts) genuinely rendered by ToolSafetyPanel.vue. */
interface RuntimeConfigSubset {
  file_broker_enabled: boolean;
  file_guard_enabled: boolean;
  read_max_lines: number;
}

interface AuditEntryDTO {
  audit_id: string;
  occurred_at: string;
  subject: { kind: "user" | "preset" | "system"; identifier: string };
  resource: { kind: "path" | "skill" | "network" | "exec" | "dep"; identifier: string };
  decision: "allow" | "deny";
  rule_id: string | null;
  correlation_id: string | null;
  note: string;
}

interface AuditRecentResponse {
  entries: AuditEntryDTO[];
}

interface ExecProfile {
  name: string;
  allowed_commands: string[];
  deny_patterns: string[];
}

interface ExecProfilesResponse {
  profiles: ExecProfile[];
  enabled: boolean;
}

interface SkillPolicyResponse {
  mode: string;
  overrides: Record<string, string>;
  last_reload: string | null;
}

interface DepBrokerPendingRequest {
  id: string;
  command_args: string[];
  requester: string;
  created_at: string;
  status: string;
}

interface DepBrokerPendingResponse {
  pending: DepBrokerPendingRequest[];
}

// ── usePolicyLists.ts `ruleField()` projection, ported verbatim so the
// expected per-category Allow-Lists content can be computed from the raw
// `PolicyResponse.rules` the same way SecurityConfigPanel.vue does. ────────

type ListField =
  | "read_allow"
  | "write_allow"
  | "write_deny"
  | "exec_allow_cwd"
  | "exec_deny_patterns";

const LIST_FIELDS: readonly ListField[] = [
  "read_allow",
  "write_allow",
  "write_deny",
  "exec_allow_cwd",
  "exec_deny_patterns",
];

const FIELD_RULE: Record<ListField, { op: PolicyRuleDTO["op"]; action: PolicyRuleDTO["action"] }> = {
  read_allow: { op: "read", action: "allow" },
  write_allow: { op: "write", action: "allow" },
  write_deny: { op: "write", action: "deny" },
  exec_allow_cwd: { op: "exec", action: "allow" },
  exec_deny_patterns: { op: "exec_deny", action: "deny" },
};

function ruleField(rule: PolicyRuleDTO): ListField | null {
  if (
    rule.action === "deny" &&
    rule.scope === "path" &&
    (rule.op === "write" || rule.op === "any")
  ) {
    return "write_deny";
  }
  for (const field of LIST_FIELDS) {
    if (field === "write_deny") continue;
    const spec = FIELD_RULE[field];
    if (rule.op === spec.op && rule.action === spec.action) return field;
  }
  return null;
}

function projectRules(rules: PolicyRuleDTO[]): Record<ListField, string[]> {
  const next: Record<ListField, string[]> = {
    read_allow: [],
    write_allow: [],
    write_deny: [],
    exec_allow_cwd: [],
    exec_deny_patterns: [],
  };
  for (const rule of rules) {
    const field = ruleField(rule);
    if (field) next[field].push(rule.pattern);
  }
  return next;
}

test.describe("Security page — CLI/API/UI consistency", () => {
  // Force the Chinese locale before every navigation in this suite. All the
  // tab labels / text assertions below are written against the confirmed
  // `zh-CN` locale strings (frontend/src/locales/zh-CN/{security,toolSafety,
  // depBroker}.ts). Without this, `resolveInitialLocale()`
  // (frontend/src/locales/index.ts) falls back to `navigator.language`,
  // which in a Playwright browser context defaults to English (no explicit
  // `locale` is set in playwright.config.ts) — so the app boots in English
  // and every Chinese lookup below times out. `qai_locale` is the exact
  // localStorage key both `locales/index.ts` and `stores/ui.ts` read on
  // boot (and the one `LanguageSwitcher.vue` persists to), so seeding it
  // via `addInitScript` reliably forces the Chinese UI regardless of the
  // host browser's default language.
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      try {
        window.localStorage.setItem("qai_locale", "zh-CN");
      } catch {
        // localStorage unavailable — the app will fall back to its own
        // navigator-language detection; nothing else to do here.
      }
    });
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

  test("策略展示: WebUI 渲染内容与 policy show 一致", async ({ page }) => {
    // UI 人工验证路径：直接导航到 /security（默认可见，无需前置开关）-> 默认
    // 落在"总览" tab，SecurityOverviewPanel.vue 的 onMounted 会无条件调用
    // store.fetchPolicy()（GET /api/security/policy）-> 其 mode/enabled 渲染
    // 在 SecurityView.vue 顶部的 .sec-header-meta 里（<strong> 包裹 mode，
    // enabled 决定 "FileGuard 已启用/已禁用" 文案）-> 再切到"白名单" tab 挂载
    // SecurityConfigPanel.vue（data-testid="security-config-panel"），它的
    // onMounted 会再发一次同一个 GET（usePolicyLists.load()），响应的
    // rules[] 按 usePolicyLists.ts 的 ruleField() 规则被投影为 5 个类别
    // （read_allow / write_allow / write_deny / exec_allow_cwd /
    // exec_deny_patterns），与 PolicyListBlock.vue 渲染的每个分类块
    // （.sec-cfg-list-block，标题为 .sec-cfg-list-key 里的字段名）逐条核对。
    const firstPolicyPromise = page.waitForResponse(
      (r) => r.url().includes("/api/security/policy") && r.request().method() === "GET",
    );
    await page.goto("/security");
    const firstResponse = await firstPolicyPromise;
    expect(firstResponse.ok()).toBeTruthy();
    const policy = (await firstResponse.json()) as PolicyResponse;

    await expect(page.locator(".sec-header-meta strong")).toHaveText(policy.mode);
    await expect(page.locator(".sec-header-meta")).toContainText(
      policy.enabled ? "FileGuard 已启用" : "FileGuard 已禁用",
    );

    // 等到总览 tab 挂载时自带的那批请求（fetchPolicy/fetchPending/…）都落地
    // 后再为"白名单" tab 即将触发的那次 GET 单独挂一个新 promise，避免误捕获
    // 到总览自己那次尚未落地的请求。
    await page.waitForLoadState("networkidle");
    const allowListsPolicyPromise = page.waitForResponse(
      (r) => r.url().includes("/api/security/policy") && r.request().method() === "GET",
    );
    await page.getByRole("tab", { name: "白名单" }).click();
    const allowListsResponse = await allowListsPolicyPromise;
    expect(allowListsResponse.ok()).toBeTruthy();
    const allowListsPolicy = (await allowListsResponse.json()) as PolicyResponse;
    const expected = projectRules(allowListsPolicy.rules);

    await expect(page.getByTestId("security-config-panel")).toBeVisible();
    for (const field of LIST_FIELDS) {
      const block = page.locator(".sec-cfg-list-block").filter({
        has: page.locator(".sec-cfg-list-key", { hasText: field }),
      });
      await expect(block).toHaveCount(1);
      const inputs = block.locator(".sec-cfg-list-row .sec-cfg-list-input");
      await expect(inputs).toHaveCount(expected[field].length);
      const values = await inputs.evaluateAll((els) =>
        els.map((el) => (el as HTMLInputElement).value),
      );
      expect(values).toEqual(expected[field]);
    }
  });

  test("技能能力发现: WebUI 渲染内容与 policy skill-cap discover 一致", async ({ page }) => {
    // UI 人工验证路径：/security -> 点击"技能"tab 挂载
    // SkillCapabilitiesPanel.vue -> useSkillCapabilities().refreshAll()
    // 在 onMounted 里并发调用 fetchDiscoveredSkills()（GET
    // /api/security/skill-discovery）等三个请求。每个技能（无论
    // feature/agent-with-policy/agent-without-policy 哪种 variant）都被
    // SkillCard.vue 渲染为一个 .sec-cfg-skill-card，其内的 .sec-cfg-skill-fid
    // 始终是原样的 skill_name（见 SkillCard.vue 的三个 template 分支）。
    await page.goto("/security");
    // 等总览 tab 自带的那批请求落地后再为"技能"tab 即将触发的这次 GET
    // 单独挂一个新 promise，避免误捕获到总览自己那次尚未落地的同 URL 请求
    // （SecurityOverviewPanel.vue 的 loadOverview() 也会调一次
    // fetchSkillDiscovery()）。
    await page.waitForLoadState("networkidle");
    const discoveryPromise = page.waitForResponse(
      (r) => r.url().includes("/api/security/skill-discovery") && r.request().method() === "GET",
    );
    await page.getByRole("tab", { name: "技能策略" }).click();
    const response = await discoveryPromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as SkillDiscoveryResponse;
    const skills = body.skills ?? [];

    const cards = page.locator(".sec-cfg-skill-card");
    await expect(cards).toHaveCount(skills.length);

    const renderedFids = await page.locator(".sec-cfg-skill-fid").allTextContents();
    const expectedNames = skills.map((s) => s.skill_name);
    expect(new Set(renderedFids.map((t) => t.trim()))).toEqual(new Set(expectedNames));

    // 确认结论（读源码后）：.sec-cfg-skill-flabel 只在 variant === "feature"
    // 时渲染，其文本是 SkillCard.vue 的 featureLabel() 对内置四个技能名做的
    // i18n 翻译结果，并非任何响应字段的原样回显——因此不对其做逐条断言，
    // 避免在测试里重复实现一份翻译映射（那会测试翻译表本身而不是一致性）。
  });

  test("安全设置: WebUI 渲染内容与 security settings get 一致", async ({ page }) => {
    // UI 人工验证路径：/security -> 点击"工具防护"tab 挂载
    // ToolSafetyPanel.vue -> onMounted 调用 useRuntimeConfig().fetchConfig()
    // （GET /api/security/runtime-config）。读源码确认：该组件真正渲染的
    // 布尔/数值开关只有 file_broker_enabled（"启用 File Broker" 复选框）、
    // file_guard_enabled（"启用 FileGuard" 复选框）与 read_max_lines
    // （"read — 最大行数" 数字输入框）三项。
    //
    // 死角说明（读源码后确认，非猜测）：sandbox_enabled 在 RuntimeConfig 类型
    // 里存在但组件注释明确写明"no UI surface drives it"（无任何界面绑定它）；
    // dependency_approval_enabled 存在于同一 DTO，但其唯一的界面开关在
    // SecurityOverviewPanel.vue 的"依赖安装代理"卡片里（useDepBroker.ts），
    // 不在本面板；native_file_guard_enabled 与 file_guard_enabled 联动写入但
    // 没有独立的界面元素展示它自身的值。issue 提到的 command_policy_enabled
    // 字段在 useRuntimeConfig.ts 的 RuntimeConfig 接口里根本不存在，因此不
    // 对上述四项做断言。
    await page.goto("/security");
    await page.waitForLoadState("networkidle");
    const runtimeConfigPromise = page.waitForResponse(
      (r) => r.url().includes("/api/security/runtime-config") && r.request().method() === "GET",
    );
    await page.getByRole("tab", { name: "工具防护" }).click();
    const response = await runtimeConfigPromise;
    expect(response.ok()).toBeTruthy();
    const config = (await response.json()) as RuntimeConfigSubset;

    const fileBrokerRow = page.locator(".tool-safety-row", { hasText: "启用 File Broker" });
    await expect(fileBrokerRow.locator('input[type="checkbox"]')).toBeChecked({
      checked: config.file_broker_enabled,
    });

    const fileGuardRow = page.locator(".tool-safety-row", { hasText: "启用 FileGuard" });
    await expect(fileGuardRow.locator('input[type="checkbox"]')).toBeChecked({
      checked: config.file_guard_enabled,
    });

    const readMaxLinesRow = page.locator(".tool-safety-row", { hasText: "read — 最大行数" });
    await expect(readMaxLinesRow.locator('input[type="number"]')).toHaveValue(
      String(config.read_max_lines),
    );
  });

  test("审计日志: WebUI 渲染条目与 audit query 一致", async ({ page }) => {
    // UI 人工验证路径：/security -> 点击"审计"tab 挂载 AuditLogPanel.vue ->
    // onMounted 无条件调用 fetchAuditLogs()（GET /api/security/audit/recent）
    // -> 无筛选条件时 filteredEntries === entries，逐条渲染进
    // .sec-cfg-audit-table 的 <tbody> 行（.sec-cfg-audit-path 是
    // resource.identifier，.sec-cfg-decision 是 decision）。这里不需要额外
    // 点击 [data-testid="audit-refresh"]——挂载本身已经触发了这次请求。
    await page.goto("/security");
    await page.waitForLoadState("networkidle");
    const auditPromise = page.waitForResponse(
      (r) => r.url().includes("/api/security/audit/recent") && r.request().method() === "GET",
    );
    await page.getByRole("tab", { name: "审计 / 授权" }).click();
    const response = await auditPromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as AuditRecentResponse;
    const entries = body.entries ?? [];

    if (entries.length === 0) {
      await expect(page.getByText("暂无审计记录")).toBeVisible();
      return;
    }

    // 审计日志表格与"临时授权"（sandbox grants）子区块共用同一个
    // .sec-cfg-audit-table 类名；审计日志表格在 DOM 中排在最前，故取 .first()。
    const table = page.locator("table.sec-cfg-audit-table").first();
    await expect(table.locator("tbody tr")).toHaveCount(entries.length);

    const renderedPaths = await table.locator(".sec-cfg-audit-path").allTextContents();
    expect(renderedPaths.map((t) => t.trim())).toEqual(
      entries.map((e) => e.resource.identifier),
    );

    const renderedDecisions = await table.locator(".sec-cfg-decision").allTextContents();
    expect(renderedDecisions.map((t) => t.trim())).toEqual(entries.map((e) => e.decision));
  });

  test("执行代理配置: WebUI 渲染内容与 exec profiles 一致", async ({ page }) => {
    // UI 人工验证路径：/security -> 点击"技能"tab 挂载
    // SkillCapabilitiesPanel.vue -> useSkillCapabilities().refreshAll() 并发
    // 调用的 fetchExecProfiles()（GET /api/security/exec_profiles）-> 渲染
    // 进面板底部"🔒 执行配置文件"只读表格（表格同样用 .sec-cfg-audit-table
    // 类名，但该 tab 下不会同时挂载审计面板，因此选择器不会冲突）。
    await page.goto("/security");
    await page.waitForLoadState("networkidle");
    const execPromise = page.waitForResponse(
      (r) => r.url().includes("/api/security/exec_profiles") && r.request().method() === "GET",
    );
    await page.getByRole("tab", { name: "技能策略" }).click();
    const response = await execPromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as ExecProfilesResponse;
    const profiles = body.profiles ?? [];

    if (profiles.length === 0) {
      await expect(page.getByText("无已加载配置")).toBeVisible();
      return;
    }

    const rows = page.locator(".sec-cfg-audit-table tbody tr");
    await expect(rows).toHaveCount(profiles.length);
    for (const [i, prof] of profiles.entries()) {
      const cells = rows.nth(i).locator("td");
      await expect(cells.nth(0)).toHaveText(prof.name || "-");
      await expect(cells.nth(1)).toHaveText(
        prof.allowed_commands.length ? prof.allowed_commands.join(", ") : "—",
      );
      await expect(cells.nth(2)).toHaveText(
        prof.deny_patterns.length ? prof.deny_patterns.join(", ") : "—",
      );
    }
  });

  test("技能全局模式: WebUI 渲染内容与 skill policy 一致", async ({ page }) => {
    // UI 人工验证路径：/security -> 点击"技能"tab 挂载
    // SkillCapabilitiesPanel.vue -> useSkillCapabilities().refreshAll() 并发
    // 调用的 fetchPolicy()（GET /api/skills/policy）-> globalMode 渲染在头部
    // 筛选栏 "全局模式：<strong>{{ globalMode }}</strong>"
    // （.sec-cfg-audit-controls .config-comment strong）。
    await page.goto("/security");
    await page.waitForLoadState("networkidle");
    const skillPolicyPromise = page.waitForResponse(
      (r) => r.url().includes("/api/skills/policy") && r.request().method() === "GET",
    );
    await page.getByRole("tab", { name: "技能策略" }).click();
    const response = await skillPolicyPromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as SkillPolicyResponse;

    await expect(
      page.locator(".sec-cfg-audit-controls .config-comment strong"),
    ).toHaveText(body.mode);
  });

  test("依赖审批: WebUI 渲染的待批准依赖与 dep pending 一致", async ({ page }) => {
    // UI 人工验证路径：/security 默认落在"总览"tab，挂载
    // SecurityOverviewPanel.vue -> onMounted 的 loadOverview() 无条件调用
    // depBroker.fetchPending()（GET
    // /api/security/dependency_approval/pending）-> 渲染进"🛡️ 依赖安装代理"
    // 卡片下的 .sec-cfg-pending-row 列表（每行的 <code class="mono"> 是
    // req.command_args.join(' ')）。
    const pendingPromise = page.waitForResponse(
      (r) =>
        r.url().includes("/api/security/dependency_approval/pending") &&
        r.request().method() === "GET",
    );
    await page.goto("/security");
    const response = await pendingPromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as DepBrokerPendingResponse;
    const pending = body.pending ?? [];

    if (pending.length === 0) {
      await expect(page.getByText("无待审批的安装请求")).toBeVisible();
      return;
    }

    const rows = page.locator(".sec-cfg-pending-row");
    await expect(rows).toHaveCount(pending.length);
    const renderedCmds = await rows.locator("code.mono").allTextContents();
    expect(renderedCmds.map((t) => t.trim())).toEqual(
      pending.map((p) => p.command_args.join(" ")),
    );
  });
});
