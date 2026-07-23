# `app-builder-consistency.spec.ts`（WebUI）

> 本文件是 `QAIModelBuilder/frontend/e2e/` 整个 Playwright 套件（套件二，共 6 个 spec 文件，不受本仓库版本控制）的说明归口，既覆盖 `app-builder-consistency.spec.ts` 自身的特有约定，也承载对整个 `e2e/` 目录都适用的通用踩坑（见文末）。QAIModelBuilder 一致性测试的两个组件（本套件 + CLI/API/channel 套件）互不共享代码，不构成公共组件，因此各自约定直接写在自己的同名文档里，没有统一的项目级归口；CLI/API/channel 套件及三端统一报告生成见 `test/test_builder_cli.md`（报告生成已内置于该脚本，不再有单独的 `generate_builder_report.py`）。

Playwright(TS)，复用前端项目自带的 `@playwright/test` devDependency（`frontend/playwright.config.ts`），驱动真实浏览器对比 WebUI 渲染结果与套件一已验证过一致性的两个只读 HTTP 端点。

**必须先绕过的三个真实产品前提**（非 bug，是测试要适配的设计事实，不要在下次改动时误当作回归）：
- 悬浮工作台面板默认隐藏（`ui.app_builder.show_workbench` 默认 `false`），需先 `POST /api/forge-config` 打开才能看到面板。
- UI 上没有"展示全部模型"的扁平入口——模型列表始终按当前选中的 taxonomy 任务分类过滤（`store.selectedTaskId`，匹配逻辑是 `model.taxonomy.includes(taskId)`）；Pack 列表用例因此按 API 返回模型的任务分类分组，逐个任务切换后核对子集，遍历完所有任务后再断言覆盖了 API 的全部模型 id，而不是假设存在一个平铺列表。
- `cc-pill`（AI 编程会话入口）在全新环境默认不可见：`ai_coding.cc.enabled` 由后端 `LoadForgeConfigUseCase._read_coding_enabled()` 派生，`ai_coding.config` KV 文档为空时明确默认 `False`（不是文档/composable 注释里说的默认 `true`），且每次 `GET /api/forge-config` 都会被重新派生覆盖，不能像工作台面板那样直接 `POST /api/forge-config` 硬写；正确做法是 `ensureCcPillVisible()` helper 里用的 `POST /api/cc/config` 写 `{"config": {"enabled": true}}`，这才是派生逻辑真正读取的持久化路径。

运行（同样在远程 ARM64，需要 Node.js ≥22 + pnpm ≥9，先 `pnpm install` + `pnpm exec playwright install chromium`）：
```powershell
cd <qaimodelbuilder源码根目录>\frontend
$env:QAI_E2E_SKIP_WEBSERVER = "1"   # 若已手动起好独立的 Builder 实例 + `pnpm dev`
pnpm exec playwright test
```
需要一个隔离的 Builder 实例（独立数据目录/端口，`QAI_AUTH__ENABLED=false`）+ 一个指向它的 Vite dev server（`frontend/.env.local` 设 `QAI_DEV_BACKEND_HTTP`/`QAI_DEV_BACKEND_WS`）。结果落在 `frontend/e2e-report/results.json`；远程运行后按惯例用 `xcopy` 拉回本机归档至 `workspace/test-results/webui_e2e_<时间戳>/`——因整套测试代码本身不受版本控制，这是它唯一的留档位置，连测试代码本身也要一并拷贝进去。

**覆盖强度的重要限制**：Pack/Run 两个用例只核对渲染出的模型名称集合/条目数量与 API 是否一致，**不逐字段比较**——因此这套验证既不构成对 D0003（CLI 缺失 API 侧 6 个字段，见 `test/test_builder_cli.md`）的验证，也不构成反驳，字段完整性层面的"同一内核"承诺尚未闭环。

**该套件不受版本控制**：`QAIModelBuilder` 整目录在 `Service/.gitignore` 中被排除，`playwright.config.ts`/全部 6 个 `*.spec.ts` 文件只存在于本机与远程机器各自的文件系统，环境重建（如远程机器更换）后需要重新放置——不要假设 `git pull` 会带上它们。

## 整个 e2e 套件通用的三个测试代码坑（首次全新环境端到端联调时发现，均已修复，均不是产品缺陷）

这三条不是某一次运行的流水账，而是这套"UI 渲染内容 == 对应 HTTP API 响应"一致性用例设计上会反复踩的坑，新增/修改任何 spec 文件前先看一遍：

- **默认英文 locale 会让所有中文文本选择器全部失效**：`frontend/src/locales/index.ts::resolveInitialLocale()` 优先读 `localStorage["qai_locale"]`，没有该值时按 `navigator.language` 判断，Playwright 默认浏览器语言不是中文，导致页面实际以英文渲染——**不是标签文案写错**（`zh-CN` locale 文件用词完全正确），根子原因是环境语言与断言语言不一致。任何依赖可见文本（而非 `data-testid`/CSS class）做选择器的 spec，`test.describe` 都应有一个 `test.beforeEach`，用 `page.addInitScript` 提前把 `localStorage["qai_locale"]` 设为 `"zh-CN"` 锁定语言（`security-consistency.spec.ts` 已这样做），不要把断言反过来改成英文。
- **后端派生字段可能在每次 GET 时被重新计算并覆盖**：不是所有"默认关闭的开关"都能像 `ui.app_builder.show_workbench` 那样一次性 `POST /api/forge-config` 写死——`ai_coding.cc.enabled` 就是反例（见上文 `cc-pill` 前提）。新增类似"先打开某个开关才能看到面板"的用例前，先确认该字段是持久化存储还是每次请求时派生，派生字段要找到真正的写入路径，而不是照抄 `ensureWorkbenchVisible()` 的模式。
- **按钮的可点击状态在页面刚加载时可能短暂且误导性地为真**：例如服务配置齿轮按钮 `canConfigure = isServiceInstalled || serviceModelsLoading`，状态请求返回前 `serviceModelsLoading` 为真、按钮暂时可点，一旦状态确认服务未安装就立刻变回禁用；在这个窗口期做 `isEnabled()` 判断会读到一个马上失效的"假可用"状态。规避方式：先 `await page.waitForLoadState("networkidle")` 等状态稳定，再读按钮的最终状态。
