# `apps/` — 应用入口层（Entry-point Layer）

> **建立于** S1 PR-010（Clean Cutover 重构）
> **对应** `docs/90-refactor/refactor-plan.md` v2.5 §6
> **状态** HTTP 入口（api/）已落地；cli/ 已有 serve；desktop/ 为预留骨架。

## 职责

- **唯一**的 `main` 所在地。所有进程入口（HTTP / CLI / Desktop）都从这里启动。
- 装配 `src/qai/` 中的限界上下文（chat / security / app_builder / ai_coding / channels /
  model_catalog / model_builder / model_runtime / tools / user_prefs / dependency_approval /
  command_policy / service_release）。
- 通过依赖注入容器（`apps/api/di.py`）把 application use case 与 adapter 接到一起；
  跨 context 协作走本层 `_*_bridge.py`。
- **不**包含业务逻辑。业务逻辑全部在 `src/qai/<context>/application/` 与 `src/qai/<context>/domain/`。

## 子目录

| 目录 | 用途 | 状态 |
|---|---|---|
| `apps/api/` | FastAPI HTTP 入口（`main` / `lifespan` / `di` + 每 context 一个 `_<ctx>_di.py` + 跨 context `_*_bridge.py`） | ✅ 已落地 |
| `apps/cli/` | 命令行入口（`serve.py`） | ✅ 已落地 |
| `apps/desktop/` | （可选）桌面单机模式入口 | ⏳ **预留骨架，仅 `__init__.py`**；当前无桌面计划，如长期不实施可删除 |

## Clean Cutover 不变性

- 本目录下任何代码 **不得** `import backend.*` / `import features.*` / `import start_server`
  （由 `check_no_legacy_import.py` 硬 guard 校验）。
- 本目录下任何代码 **不得** 引用旧 `frontend/index.html`、`frontend/js/`、`frontend/vendor/` 路径。
- `apps/api/main.py` 必须 ≤ 150 行（refactor-plan §6 硬约束）。
