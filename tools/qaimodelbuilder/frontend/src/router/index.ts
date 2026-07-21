// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Vue Router factory.
 *
 * S5 PR-050: HTML5 history mode; the legacy hash-route logic in
 * frontend/js/app.js is gone.
 * S5 PR-052: optional `installGuards()` is called from `main.ts`
 * after the i18n instance is created so the guard can resolve
 * translated titles.
 */
import { createRouter, createWebHistory, type Router } from "vue-router";
import { routes } from "./routes";
import { isChunkLoadError, handleChunkLoadFailure } from "@/utils/chunkReload";

export function createAppRouter(): Router {
  const router = createRouter({
    history: createWebHistory(),
    routes,
    scrollBehavior(_to, _from, savedPosition) {
      return savedPosition ?? { left: 0, top: 0 };
    },
  });

  // 路由切换时懒加载的视图 chunk 失败（冷启动竞态 / 旧 hash 404 / 网络抖动）
  // 走的是 vue-router 的 error 通道，不一定冒泡到 window 的 error/
  // unhandledrejection，所以这里单独兜一道：命中 chunk 失败时执行"防循环
  // reload"（逻辑与 window 级兜底共用同一套 sessionStorage 记账，互不重复）。
  router.onError((err) => {
    if (isChunkLoadError(err)) {
      handleChunkLoadFailure();
    }
  });

  return router;
}

export { installGuards } from "./guards";
