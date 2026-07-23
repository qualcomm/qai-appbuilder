import { test, expect, type Locator } from "@playwright/test";

/**
 * Skills page — WebUI / HTTP API consistency.
 *
 * See .junie/plans/qai-webui-e2e-consistency-test.md for the full design.
 * `SkillsView.vue` fetches `GET /api/skills` unconditionally in its
 * `onMounted` hook (`skillsStore.fetchSkills()`), rendering a 6-card
 * `.skeleton-card` skeleton (`.skills-grid` while `loading && skills.length
 * === 0`) until the response resolves and `.skill-card` entries replace it.
 * `page.goto("/skills")` is a fresh navigation, so setting up
 * `page.waitForResponse` before it fires reliably catches that mount fetch.
 * This page has no `data-testid` anywhere — confirmed CSS-class-only, so all
 * locators below are class-based (`.skills-grid` / `.skill-card` /
 * `.skill-card-name` / `.skill-card-id`).
 *
 * Note: `skill policy` (the `globalMode` display) is deliberately NOT
 * covered by this file — its real UI counterpart lives in the Security
 * page's `SkillCapabilitiesPanel.vue`, covered separately by
 * `security-consistency.spec.ts`.
 */

interface RawSkill {
  id?: string;
  skill_id?: string;
  name: string;
  description: string;
  enabled: boolean;
  icon?: string;
  tags?: string[];
  use_for?: string;
  skill_path?: string;
  npu_optimized?: boolean;
  mode?: "off" | "cloud" | "local" | "both";
  [key: string]: unknown;
}

interface SkillListResponse {
  skills: RawSkill[];
}

/**
 * Extract only the element's direct text nodes, ignoring any nested
 * elements. `.skill-card-name` conditionally nests a `.npu-badge` span
 * (`🔷 NPU`) inside the name div when the skill is NPU-optimized, so a
 * plain `toHaveText`/full-textContent comparison against `skill.name` would
 * fail for those cards — this mirrors how the store derives `id` too, i.e.
 * we must compare against exactly what `{{ skill.name }}` renders, not the
 * whole div's rendered text.
 */
async function directText(locator: Locator): Promise<string> {
  const text = await locator.evaluate((el) =>
    Array.from(el.childNodes)
      .filter((n) => n.nodeType === Node.TEXT_NODE)
      .map((n) => n.textContent ?? "")
      .join(""),
  );
  return text.trim();
}

test.describe("Skills — 技能列表一致性", () => {
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

  test("技能列表: WebUI 渲染内容与 skill list 一致", async ({ page }) => {
    // UI 人工验证路径：直接导航到 /skills（该页无任何前置条件/配置开关，默认
    // 可见）-> 等待骨架屏（.skeleton-card）被真实的 .skill-card 网格替换 ->
    // 对照每张卡片的名称/ID 与 GET /api/skills 响应逐一核对。
    const responsePromise = page.waitForResponse(
      (r) => r.url().includes("/api/skills") && r.request().method() === "GET",
    );
    await page.goto("/skills");
    const response = await responsePromise;
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as SkillListResponse;
    const skills = body.skills ?? [];

    // filteredSkills 在默认筛选（"all"）且搜索框为空时直接返回 skills.value
    // 本身（顺序不变），因此 .skill-card 的渲染顺序与 API 返回顺序完全一致，
    // 可按下标逐一核对。
    const grid = page.locator(".skills-grid");
    await expect(grid).toBeVisible();
    const cards = grid.locator(".skill-card");
    await expect(cards).toHaveCount(skills.length);

    for (let i = 0; i < skills.length; i++) {
      const skill = skills[i];
      const card = cards.nth(i);
      // 与 stores/skills.ts:fetchSkills() 派生 id 的逻辑保持一致：
      // id: s.skill_id ?? s.id ?? ""
      const expectedId = skill.skill_id ?? skill.id ?? "";

      await expect(card.locator(".skill-card-id")).toHaveText(expectedId);
      expect(await directText(card.locator(".skill-card-name"))).toBe(
        skill.name,
      );
    }
  });
});
