# `interfaces/` — HTTP / WS / Webhook 适配器

> **建立于** S1 PR-010（Clean Cutover 重构）
> **对应** `docs/90-refactor/refactor-plan.md` v2.5 §6
> **状态** 路由已落地（S3 起）；受 import-linter `interfaces-stays-thin` 约束。

## 职责

- **协议层**：把 HTTP/WS/SSE/Webhook 请求翻译为 application use case 调用。
- **零业务逻辑**：route handler 仅做 controller / DTO 转换 / 响应组装（受 `check_route_thinness` advisory 监控）。
- **零 framework leak**：domain / application 层不依赖 FastAPI、不依赖 pydantic。

## 子目录

```
interfaces/http/
├── routes/              # 按上下文分：chat/ security/ app_builder/ ai_coding/ 子目录 + 单文件路由
│   └── <ctx>/_dto.py    # pydantic I/O DTO 就近放在各路由域（不集中放 schemas/）
├── middleware/          # CSRF（qai_csrf / X-QAI-CSRF）、request context
├── sse/                 # SSE 帧编码器
├── ws/                  # 多标签并行对话所需 WebSocket 通道
└── error_handlers.py    # 统一异常 → HTTP 响应映射
```

> **DTO 放置约定**：pydantic I/O DTO **就近放在各路由域的 `_dto.py`**（如
> `routes/app_builder/_dto.py`、`routes/security/_dto.py`），不再集中到 `schemas/`
> 目录——后者在 PR-010 曾作为占位骨架，因实践选择 per-route `_dto.py` 已于
> 2026-06-09 删除。
>
> `apps/api/main.py` 在 lifespan 中 `app.include_router(...)` 引入
> `interfaces/http/routes/*` 中定义的 router。

## Clean Cutover 不变性

- **不保留旧 path**（除 OpenAI Compat 3 条外部契约）。
- **不**使用旧 `register_routes(app, ...)` 形式；统一用 `APIRouter()` + `app.include_router()`。
- **不**直接 import `src/qai/<context>/adapters/` 或 `infrastructure/`；只依赖 `application/ports`。
- 路由实现 **不得** 跨 context 调用业务；跨 context 协作通过 `apps/api/` 层 bridge 编排。

## S1 阶段当前状态

- ✅ 目录与 `__init__.py` 占位已落地（PR-010）。
- ⏳ `routes/system.py`（健康检查骨架）将在 PR-014 落地。
- ⏳ 业务路由（chat / security / app_builder / ai_coding / channels / model_catalog / tools）：S3 起填充。
