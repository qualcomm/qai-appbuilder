# `test_builder_cli.py`

> 本文件是 `test/test_builder_cli.py`（QAIModelBuilder 三端一致性测试的套件一：CLI + HTTP API + 微信/飞书 channel 全链路 + 统一 HTML 报告生成 + 可选的 Python Playwright CLI↔WebUI 双向黑盒验证模块，受本仓库版本控制）的文件级说明归口。与 `test_service.py` 同一种模式：一次运行即产出全部结果与报告，不再有单独的报告合并脚本。本文件的 WebUI 验证是单一、自包含的 Python `webui` 模块（模块 E，见下文）：直接用 Python Playwright 驱动浏览器，只读取渲染出的 DOM/文字，不 import 前端源码。`QAIModelBuilder/frontend/e2e/*.spec.ts`（TS Playwright，见 `QAIModelBuilder/frontend/e2e/app-builder-consistency.md`）是前端团队自己维护的一套完全独立的测试体系，仍可独立运行，但其结果不再合并进本文件的报告。

## 运行命令
本脚本本身（编排逻辑，非仅被编排的 Builder 子进程）需要 `requests` 库；远程 ARM64 机器上运行时应使用 QAIModelBuilder 官方 `Setup.bat` 搭建的独立 venv（路径见 `docs/build-guide.md` 3.1「`QAIModelBuilder`（外部应用）不随 git 同步」，已验证装有 `requests`），而不是系统默认 Python：
```powershell
"<官方venv>\Scripts\python.exe" test/test_builder_cli.py --builder_dir <QAIModelBuilder 源码根目录> --out_dir <结果输出目录>
```
关键参数（`build_arg_parser()`）：
- `--builder_python`：Builder 专用 `python.exe`；缺省调用 `resolve_builder_python()` 自动探测官方 venv。
- `--builder_host`/`--builder_port`：默认 `127.0.0.1`/`8899`。
- `--builder_data_dir`：隔离数据目录，缺省 `<out_dir>/builder_data`。
- `--modules`：只跑指定模块，可选 `cli_smoke channel consistency known_gaps webui`；缺省跑前四个。`webui` 是纯 opt-in 的第五个模块（模块 E），需要额外的 `pip install playwright && playwright install chromium` + 本机 `pnpm`，必须显式指定才会运行。
- `--start_timeout`：等待 Builder 就绪的超时秒数，默认 90。
- `--frontend_dir`：前端项目根目录（`webui` 模块专用），缺省 `<builder_dir>/frontend`。
- `--webui_base_url`：已经手动跑好的前端 dev server 地址（`webui` 模块专用）；给出时跳过自动拉起 `pnpm dev`，直接把该地址当 frontend_url 使用。
- `--webui_headed`：`webui` 模块以有头模式启动 Chromium，默认无头，调试用。

退出码：`collector.is_healthy()` 为真（`failed == ignored` 且 `crashed == 0`，标准与 `test_service.py` 一致）时退出码 0，否则 1——判定基于本次实际运行过的全部模块的结果（默认四个；若显式加了 `webui`，其结果也会计入同一个 collector），不受报告生成本身影响。同一次运行结束后，`main()` 会紧接着用刚写盘的 `results.json`/`defects.json` 自动生成报告，全部产物落在同一个 `--out_dir`：`results.json`/`defects.json`（CLI 侧原始结果，不再产出 `defects.md`——缺陷内容已直接内联进 `report.html`）+ `report.html`（单页统一报告，见下节）；远程运行后按惯例用 `xcopy` 拉回本机归档至 `workspace/test-results/builder_cli_<时间戳>/`（拉取命令见 `docs/build-guide.md` 3.1「产物回取」）。

## 四个默认模块
- **`cli_smoke`**（`run_cli_smoke_module`）：驱动真实 `qai` CLI 逐条冒烟，覆盖 `config`/`service`/`pack`/`run`/`policy`/`skill`/`service_release`/`conv`/`dep`/`code`/`app` 等命令组的只读子命令。
- **`channel`**（`run_channel_module`）：微信/飞书 channel 全链路（webhook 入站签名校验、落库、出站 mock），依赖 `builder.csrf`（`CsrfSession`）。
- **`consistency`**（`run_consistency_module`）：`_compare_cli_api_list()` 对照 CLI 输出与对应 HTTP API 响应做深度 diff（`_deep_diff`），当前覆盖 `pack list` vs `GET /api/app-builder/models`、`run list` vs `GET /api/app-builder/runs`。
- **`known_gaps`**（`run_known_gaps_module`）：已知功能缺口回归标记 + 在报告里登记本脚本自己发现的设计边界（见下节）。

## `webui` 模块（模块 E，opt-in）：CLI↔WebUI 双向验证
覆盖 `cli_smoke` 全部 36 条冒烟用例：先按 `CLI_CASE_TO_UI_EQUIVALENT` 判断是否存在网页操作入口——无入口的（`boundary_kind is not None`）由 `_webui_record_boundary_cases` 遍历映射表自动记为 `webui_py::<case>` 设计边界，不需要手写清单；有入口的 26 条按真实点击/切 tab/输入的顺序操作页面（分 6 组：安全页面、下载中心、服务页面、App Builder 工作台、最近对话+AI 编程会话、设置+技能），读取渲染结果与对应 CLI JSON 输出比对。用 Python 版 `playwright`（`pip install playwright && playwright install chromium`）直接开浏览器打黑盒，完全不 import `QAIModelBuilder/frontend` 任何源码。

**已知坑点（复现/复核前先看这里，均已在代码注释里同步标注）**：
- **Python `add_init_script()` 不会像 JS/TS 版一样自动调用传入的函数**：传 `"() => { ... }"` 这种箭头函数字符串只会创建一个函数值然后丢弃，函数体从未执行；必须直接传要执行的语句本身（如 `"window.localStorage.setItem('qai_locale', 'zh-CN');"`），或用 IIFE 包一层 `(() => {...})();`。这曾导致强制中文 locale 完全不生效，全部依赖中文文案定位的检查超时。
- **写操作接口（`POST /api/forge-config`、`POST /api/cc/config` 等）受 CSRF 中间件保护**：脱离浏览器会话、不带 `qai_csrf` cookie/`X-QAI-CSRF` header 的裸 `requests.post()` 会被 403 静默拦截（不抛异常，配置只是从未真正持久化）；必须用共享的 `builder.csrf`（`CsrfSession`，已实现双提交 Cookie 握手）发起，不要新起 `requests.Session()`。
- **App Builder 工作台默认不挂载**：渲染条件是服务端持久化配置 `ui.app_builder.show_workbench`（默认 `false`），点击『任务』模式按钮只是本地状态切换，不会自动打开这个开关；必须先用 `csrf.request` 读出 `/api/forge-config` 现有配置、合并该字段为 `True` 再写回，否则永远等不到工作台——这是刻意设计成"关闭时永久静默、不挂载、不报错"，不是产品缺陷。
- **`.ab-taxonomy-btn` 是双态开关**：不能无条件点击，若弹层因上一步关闭时序未同步仍处于展开态，再点一次会把它关掉；点击前先判断 `popover.is_visible()`，只在不可见时才点。
- **CLI JSON 顶层字段名与直觉不符**：`service-release versions`/`service-release models` 的顶层字段统一是 `items`，不是 `versions`/`models`；字段名读错时 `.get(key, [])` 永远拿到空列表且不报错，表现与"CLI 真失败"完全一样——必须显式判断 `exit_code != 0 or json_data is None` 再取值，不能只用 `.json_data or {}` 兜底。
- **`security settings get` 是设计边界，但走 `_record_known_boundary` 直接判定，不进 `CLI_CASE_TO_UI_EQUIVALENT` 静态映射表**：CLI 读的是 FileGuard 权限运行时快照，WebUI『工具防护』tab 读的是 `GET /api/security/runtime-config`，两者是完全不相关的数据模型（仅命名巧合）。
- **`policy skill-cap discover`/`conv list` 的不一致大概率是时序竞态**：WebUI 数据来自长驻的 Builder 后端进程，CLI 是本次新起的独立子进程，两次读取之间若有其它模块修改了同一份数据会出现真实但无诊断价值的不一致；出现该 defect 应先看当次 run 的时间线，不要默认怀疑比对写法。
- **Vite dev server 在 ARM64 上持续高强度无头浏览器交互下有概率原生崩溃**（退出码 `3221226505` / `0xC0000409`，`STATUS_STACK_BUFFER_OVERRUN`），崩溃后本进程存活期内所有后续 `page.goto` 都以 `net::ERR_CONNECTION_REFUSED` 失败；出现该现象先查 `frontend_stdout.log`/`frontend_stderr.log` 尾部确认退出码，这是环境层面的稀发性限制，不是选择器/比对逻辑问题。
- **每个分组的公共前置导航、以及最外层浏览器会话都必须包一层 `try/except`**：任何一处未兜底的异常会向上传播炸掉整个 `webui` 模块，连同此前已收集的其它模块结果一起丢失（`main()` 报告生成永远不会被调用）；新增分组/检查时必须遵循这个模式。

抽样/退化校验（真实环境验证时优先复核）：`pack list`/`app --json`/`run list` 只抽样前 3 个任务分组、做子集而非穷举比对；`conv list` 因侧边栏分组截断只做子集校验；`service config get` 在齿轮按钮不可点（服务未安装）时退化为只校验 CLI 输出结构；`policy show` 若 DOM 结构与假设不符会退化为只比对数量。

## 统一报告生成（与 `test_service.py::ReportGenerator` 同一模式，内置于本文件，不是单独脚本）
本次运行的全部模块（默认四个，加上可选的 `webui`）跑完、`results.json`/`defects.json` 写盘后，`main()` 立刻调用本文件内置的 `ReportGenerator`（`_common_css()` 的配色变量与卡片/徽标视觉语言直接复制自 `test/test_service.py::ReportGenerator._common_css()` 的字面文本，**不 import `test_service.py`**——保持两套独立测试体系互不 import 的既有约定，改配色需要同步手动改两处）产出**单页** `report.html`：统计卡片 + "CLI × WebUI 双向验证"表 + "新发现缺陷详情"，三者合一在同一页，不再拆分单独的 `report_defects.html`。header 区域展示本次调用的完整命令行（`_cmdline_meta_html`，默认展开的可折叠 `<details>`）——它由 `_page_shell` 渲染在 `.meta`这一行 flex 布局的**下方**、作为独立的块级区域，不与其它 meta-item 一起参与 flex 拉伸：命令行内容通常偏长，与其它 meta-item（生成时间/CLI 侧健康）混排在同一个 flex 行内会因 flex 默认的拉伸/换行规则挤出诡异的错位/居中效果，拆开渲染即可避免，改动 `_page_shell`/`_cmdline_meta_html` 时需保持这个分离。

`report.html` 顶部统计卡片：总用例数/CLI 层通过率/新发现缺陷数三张卡片恒定展示；仅当本次运行跑过 `webui` 模块（`webui_python_results` 非空）时才追加第四张"WebUI 通过率"卡片（`passed / (passed + failed)`，按定义排除 10 条设计边界/skipped）。"新发现缺陷数"卡片带 `title` 悬浮提示：该数字**只统计本次实测发现、与预期行为不符的问题**——已知设计边界（`_record_known_boundary`）与已知功能缺口（`known_gaps` 模块）均走独立的登记路径、从不进入 `DefectRegistry`，因此从不计入这个数字，不要把它误读为"全部缺陷/问题总数"。正文含两个小节：一节"CLI × WebUI 双向验证"，覆盖 `webui` 模块产出的全部结果（跑过时恒为 36 条：26 条真实浏览器点击路径比对 + 10 条设计边界；未跑该模块时表格整体替换为空态提示文案），每行的"详情"列用原生 `<details open>/<summary>` 折叠展开且**默认展开**（不是默认收起——36 条结果逐条展开查看操作路径太麻烦，默认展开可以一次性扫完全部详情，需要收起某一行时仍可手动点击折叠）；另一节"新发现缺陷详情"（锚点 `#defects-section`）按模块分组列出 `DefectRegistry` 登记的全部记录（`DefectRegistry.write()` 只写 `defects.json`）。

## `CLI_CASE_TO_UI_EQUIVALENT` 映射表与设计边界的两个子类型
该映射表现在只覆盖 `cli_smoke` 模块（模块 A）全部 36 条用例，唯一用途是供 `_webui_record_boundary_cases()` 在 `webui` 模块启动时自动识别其中 10 条没有网页操作入口/共享数据源的设计边界用例，逐一记为 `webui_py::<case>` 的 skipped 结果，不需要手写清单。`UiEquivalent` 只剩两个字段：`boundary_kind`（非空表示设计边界，见 `_BoundaryKind`）与 `boundary_reason`；其余 26 条非边界用例只需 `UiEquivalent()`（表示该用例在 `webui` 模块里有真实的浏览器点击路径比对，比对逻辑本身写在各 `_webui_check_*_group` 函数里，不依赖这份映射表）。

`boundary_kind` 分两个子类型，判断依据不是"UI 好不好用"，而是"UI 是否真的存在这个操作的载体"：
- **`no_ui_entry`**（产品本身没有对应的 UI 载体，不是"读取的数据不一样"）：`pack cache status`、`conv experience list`、`conv experience categories`（三者对应的 HTTP 端点在前端只存在于自动生成的类型声明里，没有任何组件/composable 实际调用）。
- **`no_shared_truth`**（UI 和 CLI 各自有渲染/输出，但读取的不是同一份后端数据——比"无入口"更容易被误判为遗漏，新增映射前必须先确认两者是否真的打到同一个端点）：`channel list`（CLI 在进程内直接调用仓储层 `list_by_kind()`，完全不经 HTTP；`interfaces/http/routes/channels.py` 没有任何 kind-agnostic 的"列出全部实例"路由，`cmd_list` 自身 docstring 已承认这是缺口；UI 两张卡片打的是 `GET /api/{kind}/status?instance_id=<localStorage 缓存的单一 id>`，粒度完全不同）、`config get ui.theme`（前端主题是纯 `localStorage` 状态，`ThemeToggle.vue` 不在 `/settings` 路由树内、不发 HTTP 请求，CLI 读写的后端 `user_prefs` 字段未被任何路由暴露）、`conv tab list`（`ChatTabStrip.vue` 是纯前端本地状态，CLI 读的是服务端独立的 `ConversationTab` 聚合列表）、`pack deps-status`（CLI 打全局快照接口，UI 的 `.ab-deps-badge` 打的是逐 pack 姊妹接口）、`run worker status`（同一接口被语音引擎状态点复用，语义已分岔）、`code skill list`（UI 消费的是另一个不相关的全局技能接口）、`service path`（CLI 取值来自 `InferenceService.get_install_dir()` 的静态缓存，唯一挂接的路由把返回值丢弃；UI 展示的 `exe_path` 来自 `GET /api/service/status` 的实时重新解析，取值形态和时机都不同）。

新增映射条目时先判断落在这两个子类型的哪一个，不要笼统标成同一种"边界"，否则会失去区分"确实没有入口"与"看似有入口但验证不出意义"的价值；判断口诀：先问"UI 上有没有能点的东西"（没有 → `no_ui_entry`），再问"点了之后打的接口和 CLI 是不是同一个"（不是 → `no_shared_truth`）。

## 本脚本记录的已知设计边界（`_record_known_boundary`，长期存在，不计入缺陷清单）
分类原则：长期存在、产品设计使然的限制记为"已知设计边界"，不计入缺陷清单、不要重复调查；行为与预期或早期调研假设不符的才记为下方"新发现缺陷"。
- `known_gaps::channel_message_query_missing`：官方 CLI/HTTP/仓储三层均不支持按 instance 查询 channel 消息历史，模块 `channel` 只能靠 `provider_event_id` 精确查询或直连 SQLite 旁路验证。
- `known_gaps::feishu_url_verification_missing`：`/api/feishu/webhook` 未实现 Feishu event-2.0 的 `url_verification` 挑战握手，该握手只存在于 WS 长连接路径。
- `known_gaps::wechat_outbound_not_http_mockable`：WeChat 出站完全依赖 `wechatbot` SDK 的活体 Bot 对象，不经 HTTP，无法用 HTTP mock 验证。

`pack export`/`pack validate`/`pack workspace-init` 三个子命令预期因 `scripts/build/model_builder_cli.py` 缺失触发 `ModuleNotFoundError`（`_check_known_gap_import_error`）；若该失败特征发生变化（异常类型/关键字不匹配），会被记为新缺陷而不是静默吸收——下次调整这三个子命令的断言逻辑前需先确认新的正常失败特征。

## 当前开放的新发现缺陷（本脚本职责止步于"发现 + 记录"，不修复；完整复现步骤/证据见归档的 `defects.json`，或直接看对应运行产出的 `report.html`「新发现缺陷详情」小节）
- **D0001/D0002**：`qai channel register`（飞书/微信）在非交互式 `winrs` 会话下报 Windows 凭据管理器 `CredRead` 错误（1312），疑似 DPAPI 主密钥依赖交互式登录会话，需要在交互式会话下复测才能确认是环境限制还是代码兼容性缺陷；一旦触发会连带阻塞模块 `channel` 的全部下游步骤（webhook/落库/出站 mock）。
- **D0003**：`qai pack list` 与 `GET /api/app-builder/models` 字段不一致，API 侧多出 `status`/`deps_status`/`variant_status`/`category`/`icon`/`auto_download` 六个字段——"同一内核不同外皮"承诺在这个端点上没有完全兑现。
- **D0004/D0005**：`qai pack export`/`qai pack validate` 的失败特征与早期调研假设（`ModuleNotFoundError`）不符——**该假设已被证伪**，两个子命令实测均能正常跑到业务逻辑层、报出正常的参数校验失败；已按脚本设计正确标记为"特征变化"而非静默吸收，下次调整这两个子命令的断言逻辑前需先确认新的正常失败特征。
