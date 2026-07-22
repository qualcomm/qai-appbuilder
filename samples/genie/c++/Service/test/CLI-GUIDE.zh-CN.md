# `qai` CLI 使用指南

> QAIModelBuilder 提供统一命令行入口 `qai`（`apps/cli/__main__.py`，console-script 见 `pyproject.toml`），覆盖配置、服务控制、Pack 管理、运行历史、对话/编码会话管理、安全策略、渠道（微信/飞书）、技能、依赖审批、下载中心、模型转换（Model Builder）与端侧推理（App Builder）共 14 个命令组。**CLI 与 WebUI 是同一套后端 use case 的两个平行适配器**（不是 WebUI 的子集或包装），但两者暴露的能力范围刻意不同——见第 5 节。
>
> 未安装 venv 时可用 `qai.bat <args>` 直接调用（自动定位官方 venv 的 `python.exe`）；已在 venv 内则 `qai <group> <subcommand> ...`。

## 1. 入口与基本约定

### 三个 console-script 入口

| 入口 | 绑定 | 用途 |
|---|---|---|
| `qai` | `apps.cli.__main__:main` | **唯一的统一 operator CLI**，本文档主体。 |
| `qai-serve` | `apps.cli.serve:main` | Reboot supervisor 进程，被 `Start.bat` 原样调用；等价于独立运行 `qai serve`。 |
| `qai-uninstall` | `scripts.init.uninstall:main` | 被 `Uninstall.bat` 原样调用；行为与 `qai uninstall` 一致，独立入口是为了卸载失败时的错误堆栈更干净。 |

历史上存在过的 `qai-api` / `qai-compile-factory` / `qai-pack-{export,validate}` / `qai-workspace-init` / `qai-install-qairt` / `qai-install-pack-deps` 等 console-script 已全部移除，等价行为通过 `qai <subcommand>` 复用（单入口策略，Desktop App Plan §2.4）。

### 全局用法

```cmd
qai --help                      REM 顶层帮助，列出全部命令组
qai <group> --help               REM 某个命令组的帮助
qai <group> <subcommand> --help  REM 某个子命令的完整参数
qai --repo-root <path> <group> ...   REM 覆盖仓库根目录探测（一般无需指定）
```

### 退出码约定（POSIX 风格）

| 退出码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 业务/运行时错误（use case 抛异常、DB 被锁等），traceback 打到 stderr |
| `2` | 用法错误（未知命令、缺参数、JSON 格式错误） |
| `130` | `SIGINT` 中断（`128+SIGINT`，符合 shell 惯例） |

### 一次性命令 vs 交互式会话

CLI 里两种命令的生命周期完全不同，行为差异会影响你怎么用：

- **一次性命令**（绝大多数命令组）：每次调用创建一个短生命周期 `Container`，跑完一个 use case 就关闭数据库/EventBus 连接，JSON 输出到 stdout。适合脚本化、批处理。
- **交互式会话**（仅 `qai build` 与 `qai app <pack>`）：整场会话共用同一个 `Container`，聊天流/EventBus 订阅在多轮对话之间保持存活；支持 `/` 斜杠命令；Ctrl+C 两阶段中断（第一次取消当前回合，短时间窗内第二次退出整场会话）。详见第 4 节。

## 2. 命令组速查表

| 命令组 | 类型 | 一句话作用 |
|---|---|---|
| `qai config` | 一次性 | 用户偏好 + 云端 Provider 配置向导 |
| `qai service` | 一次性 | 本地 GenieAPIService 守护进程控制（独立 CLI 子进程，非驱动正在跑的服务器） |
| `qai install-qairt` / `install-pack-deps` / `uninstall` / `compile-factory` / `api` | 一次性（顶层命令，非嵌套子组） | 环境安装、依赖安装、卸载、出厂资产编译、裸 uvicorn 调试启动 |
| `qai pack` | 一次性 | App Builder Pack 管理（列表/详情/删除/导入/依赖/缓存/分类树） |
| `qai run` | 一次性 | App Builder 运行历史（列表/详情/删除/取消/产物/导出/反馈/基准/worker 状态） |
| `qai conv` | 一次性 | 聊天会话 CRUD（列表/重命名/删除/压缩/标签/体验库/快照）——**不含流式聊天** |
| `qai code` | 一次性 | Claude Code / OpenCode 会话管理（会话/配置/凭据/技能/checkpoint/上下文）——**不含流式聊天** |
| `qai policy` / `perm` / `security` / `audit` | 一次性 | 安全体系：命令策略、权限审批队列、临时授权、审计查询 |
| `qai channel` | 一次性 | 微信/飞书渠道全生命周期（注册/配置/绑定/离线推送/WeChat 登录二维码） |
| `qai skill` | 一次性 | 聊天技能注册表 + 策略（启用范围：关闭/云端/本地/双模式） |
| `qai dep` / `exec` | 一次性 | 依赖安装审批队列 + 命令执行画像只读展示 |
| `qai service-release` | 一次性 | GenieAPIService/模型下载中心对等实现（版本/安装/删除/aria2c 控制） |
| `qai build` | **交互式会话** | Model Builder 模型转换 Agent 会话（真正的流式聊天） |
| `qai app [<pack>]` | 一次性 **或** 交互式会话 | App Builder 端侧推理（省略输入参数且在 TTY 下自动进入 REPL） |
| `qai serve` / `qai-serve` | 长驻进程（非会话） | Reboot supervisor，拉起 `apps.api` 子进程并在崩溃/`exit(75)` 时自愈重启 |

## 3. 详细命令参考

### `qai config`

| 子命令 | 作用 |
|---|---|
| `config get <key>` | 打印指定 key 的 JSON 文档 |
| `config set <key> <json>` | 浅合并 JSON 对象到指定 key |
| `config setup` | 交互式一站式向导（provider + 基础偏好），仅 TTY 下可用 |
| `config provider list` | 列出已配置 provider（`api_key` 已掩码） |
| `config provider add [id]` | 新增 provider，TTY 走向导，否则走 flag |
| `config provider edit <id>` | 编辑已存在的 provider |
| `config provider remove <id> --yes` | 删除 provider（含 SecretStore 中的 key） |
| `config provider test <id>` | 探测 provider 连通性 |

关键参数：`--type` / `--base-url` / `--default-model` / `--api-key-stdin`（从 stdin 读密钥，避免出现在 shell history）/ `--no-test`。

> **设计边界**：`qai config provider` 取代了曾经存在的 `qai model provider ...` / `qai model cloud-list`——**CLI 不管理本地端侧 LLM**（模型选择/加载走 `qai service`，不走 `qai config`/`qai model`；`qai model` 命令组已被移除）。

### `qai service`

`status` / `probe [--host --port]` / `start [--model --port --loglevel]` / `stop` / `load-model <name>` / `models [--models-root]` / `logs [--lines]` / `logs-clear` / `path` / `config get` / `config set <json>`。

> **易踩坑**：`qai service start` 是**进程内（in-process）**实现——它从当前 CLI 进程发起、启动**自己的** GenieAPIService 子进程，并不会改变某个正在运行的 `apps.api` 服务器内部的适配器状态。要驱动"正在运行的服务器"，应改用 HTTP API（该能力在 CLI 侧仍是待办）。

### `qai install-*` / `qai uninstall` / `qai compile-factory` / `qai api`（顶层命令）

| 命令 | 作用 |
|---|---|
| `install-qairt [...]` | 安装 QAIRT SDK + x64-py310 venv（透传给 `scripts.init.install_qairt`） |
| `install-pack-deps [...]` | 安装 App Builder Pack 依赖（透传给 `scripts.setup.install_app_builder_deps`） |
| `uninstall [...]` | 卸载器（透传给 `scripts.init.uninstall`） |
| `compile-factory [...]` | 编译出厂资产（透传给 `scripts.build.compile_factory`） |
| `api [...]` | 裸 uvicorn 直启 FastAPI（**无** reboot supervisor，仅调试用；生产场景用 `qai serve`） |

这四个命令用 `add_help=False` + 特殊 `prefix_chars` 禁用 argparse 自身的选项解析，把全部 token（含 `--help`）原样透传给底层脚本自己的 `main(argv)`——CLI 本身不重新解析任何 flag，避免帮助文本与底层脚本产生漂移。

### `qai pack`

| 子命令 | 作用 |
|---|---|
| `list` | 列出 DB 中注册的全部 Pack |
| `show <id>` | 打印指定 Pack 的模型定义 |
| `delete <id> --yes` | 删除用户导入的 Pack（内置 Pack 受保护） |
| `import --dry-run \| --apply \| --rollback` | 三选一互斥组的导入工作流 |
| `manifest <id>` | 打印解析后的 Pack manifest |
| `deps-status [id]` | 依赖状态快照（全局或单 Pack） |
| `deps-install <id>` | 安装该 Pack 的 Python 依赖 |
| `cache status` / `cache clear` | 结果缓存查询/清空 |
| `taxonomy` | 打印分类树（group, task） |
| `export [...]` / `validate [...]` / `workspace-init [...]` | 透传给 `scripts.build.model_builder_cli` |

### `qai run`

`list [--model --limit]` / `show <run-id>` / `delete <run-id>` / `cancel <run-id> [--reason]` / `artifacts <run-id>` / `export <run-id> [--out]` / `feedback <run-id> --rating [--note]` / `bench <benchmark-id>` / `worker status`。

> `worker status` 挂在 `run` 下而非独立顶层 `qai worker`，是因为命令组注册机制不允许非 `__main__.py` 模块注册"兄弟顶层动词"而不改 `__main__.py`。**长驻的流式端点（`POST /runs` 等）刻意不在 CLI 暴露**——那是 API 服务器的职责，见 `qai api`。

### `qai conv`（聊天会话管理，非流式）

| 子组 | 子命令 |
|---|---|
| 会话 | `list [--query --limit --offset]` / `show <id> [--cursor --limit]` / `rename <id> <title>` / `delete <id> --yes` / `compact <id> [--budget --threshold --model-id]` / `generate-title <id> <user_message> [...]` |
| `tab` | `tab list` / `tab open <conv-id> [--tab-id]` / `tab close <tab-id>` |
| `experience` | `experience list [--category --limit]` / `experience delete <id>` / `experience categories` |
| `snapshot` | `snapshot get <request_id>` / `snapshot save <request_id> <json>` |

> **设计边界**（源码原文）：*"Streaming / server-only use cases (`StreamChatUseCase` / `UploadImageUseCase` / `EnhancePromptUseCase` / `BuildMemoryContextUseCase`) are deliberately excluded — they require a long-lived API server / WebSocket pipe / inbound HTTP request body."* 需要真正聊天时用 `qai serve` + WS/SSE API，或用 `qai build`（专用于模型转换场景，见第 4 节）。

### `qai code`（Claude Code / OpenCode 会话管理，非流式）

| 子组 | 子命令 |
|---|---|
| `session` | `list [--scope]` / `show` / `history` / `rename` / `activate` / `effort <level>` / `notify <channel> <toggle>` / `delete --yes` / `truncate <marker_index> [...]` / `workspace <path>` / `terminate [--reason]` / `interrupt` / `abort` / `revert <marker_index>` |
| `config` | `get --provider {cc,oc}` / `set --provider {cc,oc} <json>` |
| `creds` | `list --provider` / `set --provider --key --value-stdin` / `delete --provider --key` |
| `oc`（OpenCode 子进程） | `status` / `start` / `stop [--force]` / `logs [--lines]` |
| `skill` | `register <json>` / `list` |
| `checkpoint` | `create <session_id> [--note]` / `list` / `rewind <checkpoint_id>` |
| `context` | `usage <session_id>`（CC 风格）/ `size <session_id>`（OC 风格） |
| 顶层散件 | `health [--provider --refresh]` / `perm expire <session_id>` |

> **设计边界**（源码原文）：*"Streaming / interactive use cases (`StreamCodingSessionUseCase` / `SendUserMessageUseCase` / `StreamToolExecUseCase`) are intentionally excluded — they require a long-lived API server pipe."* 与 `code.py` 没有 REPL 是同一个决策。

### `qai policy` / `qai perm` / `qai security` / `qai audit`（安全体系，四个同级顶层组）

| 组 | 子命令 |
|---|---|
| `policy` | `show` / `set --file <rules.json> [--yes --reason]`（触发服务器重启）/ `apply-template {demo,development,strict} [--yes]` / `skill-cap discover \| policy <name> \| register <json> \| unregister <name>` |
| `perm` | `list` / `approve <id> [--reason --decided-by]` / `reject <id> [...]` / `cancel <id> [--cancelled-by]` / `check <json>` |
| `security`（原名 `sandbox`） | `grant <json>` / `revoke <grant_id> [--revoked-by]` / `settings get` |
| `audit` | `query [--limit --filter]`（`--filter` 目前是预留占位，尚未接入查询后端） |

`policy set` / `policy apply-template` 会触发服务器重启信号（`REBOOT_EXIT_CODE=75`），因此强制要求显式 `--yes`。`qai policy skill-cap` 与 `qai skill`（第 3 节下方）是**两个刻意分离的体系**（分别对应 security 上下文的能力注册 vs user_prefs 上下文的技能开关），不要混淆。

### `qai channel`（微信 / 飞书）

- 通用：`register --kind {feishu,wechat} --name [--secret-value --meta]` / `list [--kind]` / `show <id>` / `status <id>`（`show` 别名）/ `delete <id> [--yes]` / `start <id>` / `stop <id>` / `acknowledge <id>`
- `config get/set <id>`、`proxy get/set <id>`、`model get/set <id>`、`binding list/set/delete <id>`、`session bind/lookup <id>`
- `push <id> --user-id (--text|--text-stdin) [--page-format]` —— **单向离线消息推送，不是双向聊天**：只发一条文本给某个 IM 用户，不接收回复、不维持对话上下文。
- WeChat 专属：`wechat login/qr/qr-status/qr-issue/logout <id>`

> **已知限制**（源码原文记录）：`channel start`/`stop` 无法信号一个跨进程独立运行的 daemon；`channel delete` 没有专用 use case，直接调仓储层；部分 V1↔V2 差异项被记录为待办而非本 CLI 范围。

### `qai skill`

`list` / `policy` / `policy-mode <mode>` / `toggle <name> {on,off}` / `mode <name> {off,cloud,local,both}` / `reload`。

### `qai dep` / `qai exec`

- `dep`：`pending` / `approve <request_id>` / `reject <request_id>`
- `exec`：`profiles`（只读，展示执行策略的静态画像）

两组注册在同一个模块（`commands/dep.py`），因为共享同一套"沙箱允许执行什么命令"的概念框架。

### `qai service-release`（下载中心对等实现）

`versions` / `models` / `install {service,model} <archive>` / `delete {service,downloaded,model} <target>` / `status {versions,models}` / `aria2c {status,start,stop,cancel <task_id>}` / `settings {get,set <json>}`。

`delete service <version>` 若该版本仍在运行：非 TTY 场景必须显式加 `--stop`/`--yes`；TTY 场景弹出阻塞式 y/N 确认。**流式下载进度刻意不在 CLI 暴露**（那是给 API 服务器 SSE 用的），CLI 只暴露 `aria2c status/start/stop/cancel` 做旁路控制。

## 4. 交互式会话细节

### `qai build` —— Model Builder Agent 会话（真正的流式聊天）

这是 CLI 里**唯一**"像 WebUI 聊天页面一样，实时发一条消息并流式收回复"的入口：非斜杠输入的每一行都会作为自然语言消息，经与 WebUI 完全同一份 `stream_chat_use_case` 发起流式对话，逐帧渲染到终端（`chunk`/`tool_call`/`tool_result`/`error`/`end` 等 13 种帧类型）。

关键参数（刻意拆分，避免同名歧义）：

| 参数 | 含义 |
|---|---|
| `--model-file` / `-f <path>`（可重复） | 要**转换**的模型文件 |
| `--llm <model-id>` | Agent 使用的**云端大模型**（模型转换场景仍需要一个 LLM 来编排流程） |
| `--precision <csv>` | 七档量化精度（如 `fp16,w8a8`） |
| `--dataset <path>` | 校准数据集路径 |
| `--mode {batch,interactive}` | 运行模式 |
| `--resume [<conversation-id>]` | 续接会话，不带参数则 best-effort 续接最近一个 |

REPL 内全部斜杠命令：

| 命令 | 作用 |
|---|---|
| `/help`（`/?`） | 显示全部命令 |
| `/model <path...>` | 设置/查看要转换的模型文件 |
| `/precision <csv>` | 设置/查看量化精度 |
| `/dataset <path>` | 设置/查看数据集 |
| `/params` | 查看当前模型/精度/数据集/模式 |
| `/mode {batch,interactive}` | 切换模式 |
| `/run` | 用当前参数发起一次转换指令（真正向 Agent 发消息） |
| `/retry` | 重发上一条用户消息 |
| `/stop` | 中止当前回合 |
| `/history` | 打印会话历史消息 |
| `/clear` | 开启新会话 |
| `/exit`（`/quit`） | 退出会话 |
| `/status` / `/workspace` / `/promote` | **尚未接通**（Model Builder 后端用例暂不可从 CLI 触达，会打印明确提示而非崩溃；`/promote` 提示改用 `qai pack import`） |

### `qai app [<pack>]` —— App Builder 推理（一次性 或 REPL）

不带 `<pack>` 时列出全部可用 Pack。两种形态共用同一条执行引擎（`RunAppUseCase`），本质是"给模型一个输入，取回一个结构化输出"，**不是与 Agent 对话**：

- **一次性**：`qai app <pack> --image/--audio/--text <value|-> [--variant --param key=val ... --json --out --save-annotated]`（`-` 表示从 stdin 读取）。
- **REPL**：省略主输入 flag 且处于 TTY 时自动进入；非 TTY 且无输入 flag 则报用法错误（退出码 2）。

REPL 内全部斜杠命令：`/model <pack>`、`/variant <id>`、`/param key=val`、`/params`、`/examples [序号] [覆盖输入]`、`/history`、`/last`、`/out <path>`、`/help`、`/exit`（`/quit`）。非斜杠行会被当作该 Pack 下一次推理的输入。

## 5. CLI 刻意不支持的能力（设计边界）

以下能力在源码注释中被**明确记录为设计上刻意排除**，不是遗漏——遇到"CLI 里怎么没有 X"时先查这张表：

| 能力 | 排除原因 | 替代方式 |
|---|---|---|
| 本地端侧 LLM 的选型/云端-本地切换管理 | CLI 不管理 on-device LLM（`qai model` 组已移除） | 模型加载走 `qai service load-model`；选型/切换在 WebUI 完成 |
| `qai conv` 下的实时聊天 | 需要长驻 API 服务器 / WS 管道 / 入站 HTTP 请求体 | `qai serve` + WS/SSE API，或改用 `qai build`（限模型转换场景） |
| `qai code` 下的实时聊天/工具执行流 | 同上 | `qai serve` + WS/SSE API |
| `qai run` 暴露长驻流式端点（如 `POST /runs`） | 那是 API 服务器职责 | `qai api` / `qai serve` |
| `qai service-release` 的下载进度流式展示 | SSE 迭代器为 API 服务器设计 | `qai service-release aria2c status` 旁路查询 |
| `qai channel` 跨进程信号独立 daemon 的 start/stop | 已知限制，跨进程无法可靠信号 | 记录为待办，暂无 CLI 侧替代 |

## 6. CLI 扩展建议（现状分析，非既定计划）

> 本节是基于当前 WebUI 功能盘点得出的**建议**，不代表已排期或已决策；用于后续规划 CLI 能力时参考。

WebUI 里以下功能复杂度高、当前 CLI 完全没有对等命令，按"价值 / CLI 化难度"排序：

1. **多 Agent 讨论模式的核心开关与角色管理**——讨论模式开关、发言人策略（`manager`/`round_robin`）、轮数上限、参与者增删改（含模型/persona/工具集），这套能力在 WebUI 是聊天核心功能之一，但 CLI 的 `qai conv`/`qai build` 完全没有覆盖；即使只暴露 CRUD（不追求 WebUI 的引导式弹窗体验），也能补上"纯命令行跑多 Agent 辩论"这一使用场景的空白，建议优先级最高。
2. **Agent/Roster/Mode 模板库的 CRUD**——三层模板当前只能通过 WebUI 弹窗管理；补一组 `qai template {agent,roster,mode} list/show/create/clone/delete` 之类的一次性命令即可覆盖大部分场景（不需要还原"克隆自动进入编辑态"这种引导流程）。
3. **权限审批的批量操作**——CLI 现有 `qai perm approve/reject/cancel` 已支持单条操作；WebUI 的"全部允许(session)/全部拒绝/取消同进程全部请求"等批量能力目前 CLI 没有对应的批量 flag，值得补一个 `--all` 或按 PID 批量的选项。
4. **审计日志的多维过滤查询**——`qai audit query` 的 `--filter` 目前是预留占位，尚未接入过滤后端；WebUI 已有 6 维过滤能力，把这套过滤条件接到 CLI 上是相对低成本的补齐。
5. **Cloud Models Provider 的逐参数支持开关**（temperature/top_p/max_tokens/thought_signature）——`qai config provider` 目前只覆盖 base-url/api-key/model 基本字段，这几个细粒度开关建议作为 `provider edit` 的新增 flag 补上，避免用户只能靠裸 JSON `config set` 硬改。

以下 WebUI 功能**不建议**引入 CLI（收益低或场景本身与命令行不契合）：

- **App Builder 分享链接（Share Link）**——纯 Web 场景产物（生成可公开访问的 URL），命令行用户没有"拿链接分享给别人在浏览器打开"的天然需求。
- **下载中心的实时进度条/速度/ETA 可视化**——`qai service-release aria2c status` 已能拿到等价数据，CLI 场景下用一行文本刷新展示即足够，不必模拟 Web 的进度条视觉。
- **App Builder Benchmark 触发 UI**——注意这不是"WebUI 有、CLI 没有"，而是**后端 API 契约存在、WebUI 自己也没有调用它的 UI**（详见附注）；`qai run bench <benchmark-id>` 已是 CLI 侧的对应能力，是否要补齐反而应该先问 WebUI 团队要不要把这个契约接上前端。

> **附注**：WebUI 盘点过程中发现 `POST /api/app-builder/benchmark` 有完整的后端契约（p50/p90/p99/min/max/mean/std），但整个前端代码库中找不到任何组件调用它——这是三端（后端契约、WebUI、CLI）中 WebUI 自己先缺失的功能，不属于"该给 CLI 补的功能"范畴，仅记录于此供参考。
