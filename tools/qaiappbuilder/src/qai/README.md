# `src/qai/` — 业务源码包根（src layout）

> **建立于** S1 PR-010（Clean Cutover 重构）
> **对应** `docs/90-refactor/refactor-plan.md` v2.7 + §3
> **状态** 重构主体完成（post-S9 + edition 双形态）；14 个限界上下文四层已落地。

## 顶层结构

```
src/qai/
├── platform/            # 共享内核（最内层基础设施抽象）
│   ├── config/          # 配置抽象 + 加载（pydantic-settings 落点）
│   ├── logging/         # 结构化 logger
│   ├── errors/          # 错误层级（DomainError / AppError / InfraError）
│   ├── events/          # 进程内事件总线（asyncio）
│   ├── ids/             # ULID/UUID 生成
│   ├── time/            # Clock 抽象（便于测试）
│   ├── io_validator/    # I/O 校验工具
│   └── persistence/     # SQLite 抽象 / 迁移
├── chat/                # 限界上下文：聊天（含多标签会话、多 Agent 讨论）
├── ai_coding/           # 限界上下文：AI Coding（CC / OC sessions）
├── app_builder/         # 限界上下文：App Builder（端侧 Model Pack 执行）
├── model_builder/       # 限界上下文：模型构建（转换 + 导入 App Builder）
├── model_catalog/       # 限界上下文：模型目录与下载（远程 release manifest）
├── model_runtime/       # 限界上下文：GenieAPIService daemon 控制
├── security/            # 限界上下文：安全（PolicyCenter 拆分后的子领域）
├── channels/            # 限界上下文：渠道（feishu / wechat 单实例）
├── tools/               # 限界上下文：工具执行
├── user_prefs/          # 限界上下文：持久用户偏好（kv_user_prefs）
├── dependency_approval/  # 限界上下文：依赖安装审批队列
├── command_policy/        # 限界上下文：命令执行审批 profile
└── service_release/     # 限界上下文：GenieAPIService 下载中心
```

> 注：早期骨架曾含 `iam/`（占位命名空间，零代码引用），已于 D11/A-4 决议删除（连同
> `layered-iam` 契约）；现行 import-linter 共 17 contracts。

## 每个限界上下文的标准四件套

```
<context>/
├── domain/              # 实体 / 值对象 / 领域服务（无 I/O）
├── application/         # use case + ports（依赖反转）
├── adapters/            # 实现 ports（HTTP client / SDK / OS API）
└── infrastructure/      # 持久化 / 缓存 / 文件系统
```

> Domain 层不允许 import `application` / `adapters` / `infrastructure` / `apps` / `interfaces`。
> Application 层不允许 import `adapters` / `infrastructure` / `apps` / `interfaces`，但可以依赖 ports（abstract）。
> Adapters / Infrastructure 实现 application 的 ports。
> 这些规则在 PR-085 通过 import-linter 强制。

## Clean Cutover 不变性

- 本目录下任何代码 **不得** `import backend.*`、`import features.*`、`import start_server`。
- 本目录下任何代码 **不得** 出现 `print(`、`except Exception: pass`、模块级 monkey-patch、模块级可变全局。
- 旧字面量（`"data/"`、旧 SQLite 文件名、旧 manifest 文件名）**不得** 出现；路径必须经 `qai.platform.config.paths` 端口。
- 业务能力 SLA 见 [`docs/90-refactor/inventory/08-business-capabilities.md`](../../docs/90-refactor/inventory/08-business-capabilities.md)。

## 当前状态

- ✅ 重构主体已完成（S0..S9 + 9 缺口闭环 + edition 双形态）；14 个限界上下文的
  domain / application / adapters / infrastructure 四层均已落地。
- ✅ import-linter 17 contracts 全过（layered-`<ctx>` 单向 + domain-purity +
  context-isolation + interfaces-stays-thin + no-legacy-deps）。
- 最新阶段快照见 `docs/90-refactor/HANDOFF-after-edition-dual-form-2026-06-16.md`；
  待办的唯一真值源见 `docs/90-refactor/PENDING-WORK.md`。
