// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Application entry point.
 *
 * S5 PR-050: scaffolds the Vite + Vue 3 SFC + Pinia + vue-router +
 * vue-i18n composition. Business logic lives in PR-051..056; this file
 * stays narrow and stable.
 */

import { createApp } from "vue";
import App from "./App.vue";
import { createAppRouter } from "./router";
import { createAppPinia } from "./stores";
import { createAppI18n } from "./locales";
import { installChunkReloadGuard } from "./utils/chunkReload";
import { installCodeBlockThemes } from "./styles/codeBlockThemes";

import "./styles/base.css";

// highlight.js code-block themes — bundled via Vite CSS pipeline (PR-056).
// Replaces the legacy vendor/github.min.css & vendor/github-dark.min.css.
//
// NOTE: the two themes are NOT imported as plain global CSS here. Doing so let
// the last-imported (dark) theme win in BOTH app themes, so code blocks and
// tool-result diffs (write/edit/apply_patch) stayed dark even in light mode.
// `installCodeBlockThemes()` instead scopes each theme under `html.light` /
// `html:not(.light)` so highlight.js follows the app theme. See that module.
installCodeBlockThemes();

// 入口级全局兜底：捕获懒加载 chunk（路由组件 / locale / 大依赖）的动态 import
// 失败（冷启动竞态 ERR_NETWORK_CHANGED / 旧 hash 404 / 网络抖动），在防循环
// 控制下自动 location.reload() 一次重新拉取，免去用户手动刷新。详见
// utils/chunkReload.ts。必须在 createApp 之前安装，确保最早的 chunk 失败也被兜。
installChunkReloadGuard();

const app = createApp(App);

app.use(createAppPinia());
app.use(createAppI18n());
app.use(createAppRouter());

app.mount("#app");
