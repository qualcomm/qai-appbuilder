# `frontend/src/` — 新前端（Vite + Vue 3 SFC）骨架

> **建立于** S1 PR-010
> **完整实施** S5 PR-050 ~ PR-056
> **对应 plan** `docs/90-refactor/refactor-plan.md` v2.5 §11

## 双布局共存说明（S1 ~ S5）

`frontend/` 同时包含：

- **旧 no-build 前端**（S8 PR-081 删除）：
  - `frontend/index.html`（3,099 行）
  - `frontend/js/`（含 `app.js` 3,672 / `useChat.js` 3,170 等）
  - `frontend/css/`、`frontend/locales/`、`frontend/vendor/`
- **新 Vite 前端**（S1 起增量填充）：
  - `frontend/src/main.ts`（PR-050）
  - `frontend/src/App.vue`、`frontend/src/router/`、`frontend/src/stores/`、`frontend/src/composables/`、`frontend/src/api/`、`frontend/src/views/`、`frontend/src/components/`、`frontend/src/locales/`、`frontend/src/styles/`、`frontend/src/types/`
  - `frontend/public/`
  - `frontend/index.html`（在 PR-081 删除旧版后会被新 Vite 入口 `frontend/index-new.html` → 改名 `index.html` 替换）

> **重要**：S5 之前**不**接触旧 `frontend/index.html`、`frontend/js/`、`frontend/css/`、`frontend/locales/`、`frontend/vendor/`。新前端先在 `frontend/src/` 内独立开发，使用独立的入口（如 `frontend/index-new.html`）；S8 PR-081 时再做切换与删除。

## 技术栈（PR-011 / PR-050 锁定）

- Vue 3.4+（SFC + `<script setup>` + Composition API）
- Vite 5+
- TypeScript 5.4+
- Pinia（替代旧散落 reactive store）
- vue-i18n
- vue-router
- 工具库：marked、highlight.js、DOMPurify（与旧版相同；从 npm 引入）
- Node 22+ / pnpm 9+

## Clean Cutover 不变性

- 新前端代码 **不得** 引用 `frontend/js/`、`frontend/vendor/`、`frontend/index.html`（旧）。
- 新前端代码 **不得** 通过 `<script src="/vendor/...">` 加载运行时库；必须 npm import。
- 业务层 **不得** 直接 `fetch(`；必须经 `frontend/src/api/{http,sse,blob}.ts`。
- 新代码要求 strict TypeScript（`strict: true` + `noUncheckedIndexedAccess: true`）。

## 业务能力 SLA

新前端必须覆盖以下 17 个 view-level（详见 [`docs/90-refactor/inventory/04-frontend.md`](../../docs/90-refactor/inventory/04-frontend.md) §A）：

- 主聊天视图 / 多标签会话切换 / 历史侧栏
- App Builder 视图（项目列表 / 编辑 / 构建 / 运行结果）
- AI Coding 视图（CC / OC 会话）
- Channels 管理（feishu / wechat 启停 + 配置 + 二维码）
- Model Catalog / Download 视图
- Security / Sandbox 设置（含 Persistent ACL 编辑器、Pending requests 通知）
- 经验库 / 全局设置 / Forge / 服务设置 / 技能管理

## S1 阶段当前状态

- ✅ 目录骨架（PR-010）。
- ⏳ `package.json` / `vite.config.ts` / `tsconfig.json`（PR-011）。
- ⏳ `main.ts` / `App.vue` / 路由空骨架（PR-050）。
- ⏳ 业务视图：PR-051 ~ PR-056。
