// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * chunkReload — 动态 import chunk 加载失败的全局兜底。
 *
 * 背景（根因）：SPA 用 Vite 构建，路由组件 / locale 文件 / 大依赖都是
 * 懒加载 chunk（`() => import(...)`）。这些 chunk 会在几种情况下加载失败：
 *  - 冷启动竞态：后端刚就绪、浏览器抢跑并发拉一批 chunk，撞上后端 event
 *    loop 繁忙窗口（voice warmup + usage 上报），请求被中断 →
 *    `net::ERR_NETWORK_CHANGED`。
 *  - 后端重启 / 前端重新构建后，旧标签页仍持旧 hash 的 chunk 名 → 404。
 *  - 网络抖动（Wi-Fi/VPN 切换）。
 * 浏览器侧的最终表现都是动态 import 失败，抛出 `ChunkLoadError` 或
 * `TypeError: Failed to fetch dynamically imported module` /
 * `Importing a module script failed`。在没有兜底时，用户必须手动刷新才恢复。
 *
 * 治本兜底：捕获动态 import 失败 → `location.reload()` 重新拉取（带新的、
 * 正确的 chunk hash）。但**无脑 reload 会抖动**（坏构建 / 持续 404 会把用户
 * 卡进无限重载循环），因此用 `sessionStorage` 做"防循环"控制：
 *  - 仅在"距上次自动 reload > COOLDOWN_MS"且"本会话自动 reload 次数 <
 *    MAX_RELOADS"时才 reload；
 *  - 超限则放弃 reload（仅 console.warn），把控制权交还用户，避免死循环。
 *
 * 这套与 🔴 State-Truth-First 一致：reload 的依据是"真实的 import 失败事件"，
 * 防循环依据是"sessionStorage 里真实记录的上次 reload 时间/次数"，而非进程内
 * 乐观假设。
 */

/** sessionStorage key：上一次自动 reload 的时间戳（epoch ms）。 */
const RELOAD_AT_KEY = "__chunk_reload_at";
/** sessionStorage key：本会话已自动 reload 的累计次数。 */
const RELOAD_COUNT_KEY = "__chunk_reload_count";

/** 两次自动 reload 之间的最小冷却窗口（ms）。 */
const COOLDOWN_MS = 10_000;
/** 单个会话允许的最大自动 reload 次数（超出即放弃，防死循环）。 */
const MAX_RELOADS = 2;

/**
 * 判定一个错误是否为"动态 import / chunk 加载失败"。
 *
 * 覆盖各主流浏览器与打包器的措辞：
 *  - Vite/Rollup 注入的 `ChunkLoadError`（`error.name === "ChunkLoadError"`）。
 *  - Chromium：`Failed to fetch dynamically imported module`。
 *  - Firefox：`error loading dynamically imported module`。
 *  - Safari/WebKit：`Importing a module script failed`。
 *  - Webpack 风格：消息内含 `Loading chunk ... failed` / `Loading CSS chunk`。
 *
 * 入参类型为 `unknown`，因为它可能来自 `ErrorEvent.error`、
 * `PromiseRejectionEvent.reason` 或 `router.onError` —— 形态不固定。
 */
export function isChunkLoadError(err: unknown): boolean {
  if (err === null || err === undefined) {
    return false;
  }

  // 1) 标准 Error 的 name 命中（Vite 把这类失败包成 ChunkLoadError）。
  if (typeof err === "object" && "name" in err) {
    const name = (err as { name?: unknown }).name;
    if (name === "ChunkLoadError") {
      return true;
    }
  }

  // 2) 取出可读的消息文本（Error.message 或直接的字符串错误）。
  let message = "";
  if (typeof err === "string") {
    message = err;
  } else if (typeof err === "object" && "message" in err) {
    const m = (err as { message?: unknown }).message;
    if (typeof m === "string") {
      message = m;
    }
  }
  if (message === "") {
    return false;
  }

  const needles = [
    "Failed to fetch dynamically imported module",
    "error loading dynamically imported module",
    "Importing a module script failed",
    "Loading chunk",
    "Loading CSS chunk",
    "ChunkLoadError",
  ];
  return needles.some((n) => message.includes(n));
}

/**
 * 纯函数：根据"上次 reload 时间 / 已 reload 次数 / 当前时间"判定是否应当再
 * reload。抽成纯函数便于单测覆盖防循环逻辑，不触碰真实 `sessionStorage` /
 * `location`。
 *
 * 规则：必须同时满足
 *  - 已 reload 次数 < MAX_RELOADS（防坏构建无限重载）；
 *  - 距上次 reload 已超过 COOLDOWN_MS（防一批 chunk 同时失败时连环 reload）。
 */
export function shouldReload(
  lastReloadAt: number | null,
  reloadCount: number,
  now: number,
  cooldownMs: number = COOLDOWN_MS,
  maxReloads: number = MAX_RELOADS,
): boolean {
  if (reloadCount >= maxReloads) {
    return false;
  }
  if (lastReloadAt !== null && now - lastReloadAt < cooldownMs) {
    return false;
  }
  return true;
}

/** 安全读取 sessionStorage 里的数字（缺失 / 非法 → 回退值）。 */
function readNumber(key: string, fallback: number): number {
  try {
    const raw = window.sessionStorage.getItem(key);
    if (raw === null) {
      return fallback;
    }
    const n = Number.parseInt(raw, 10);
    return Number.isFinite(n) ? n : fallback;
  } catch {
    // sessionStorage 在隐私模式 / 受限沙箱下可能抛异常；视为"无记录"。
    return fallback;
  }
}

/**
 * 命中 chunk 加载失败时调用：在防循环允许的前提下记录状态并 `location.reload()`。
 *
 * @returns 是否真的触发了 reload（false = 被防循环拦截 / 环境不支持）。
 */
export function handleChunkLoadFailure(): boolean {
  if (typeof window === "undefined" || typeof window.location === "undefined") {
    return false;
  }

  const now = Date.now();
  const lastReloadAtRaw = readNumber(RELOAD_AT_KEY, Number.NaN);
  const lastReloadAt = Number.isFinite(lastReloadAtRaw)
    ? lastReloadAtRaw
    : null;
  const reloadCount = readNumber(RELOAD_COUNT_KEY, 0);

  if (!shouldReload(lastReloadAt, reloadCount, now)) {
    // 超出冷却 / 次数上限：不再 reload，避免把用户卡在无限重载循环里。
    // 仅警示，由用户决定是否手动刷新。
    console.warn(
      "[chunkReload] 动态 import 反复失败，已达自动重载上限，停止自动刷新。" +
        "若界面异常请手动刷新或检查网络/后端。",
    );
    return false;
  }

  try {
    window.sessionStorage.setItem(RELOAD_AT_KEY, String(now));
    window.sessionStorage.setItem(RELOAD_COUNT_KEY, String(reloadCount + 1));
  } catch {
    // 写不进 sessionStorage（隐私模式等）时仍允许本次 reload，但失去防循环
    // 记账能力 —— 此时退而依赖浏览器自身的"反复失败"行为，风险可接受。
  }

  window.location.reload();
  return true;
}

/**
 * 安装入口级全局兜底：监听 window 的 `error` 与 `unhandledrejection`。
 *
 * 动态 import 失败有两条逃逸路径：
 *  - 作为脚本加载错误冒泡到 `window.onerror`（`ErrorEvent`）。
 *  - 更常见地，作为未捕获的 Promise rejection（`import()` 返回 Promise，
 *    在 await/调用点没 catch 时走 `unhandledrejection`）。
 * 两者都要兜。`router.onError` 是第三条通道（路由切换时的 chunk 失败），在
 * router 工厂里单独挂（见 router/index.ts），因为它不一定冒泡到 window。
 *
 * @returns 卸载函数（移除监听器），便于测试或热重载场景清理。
 */
export function installChunkReloadGuard(): () => void {
  if (typeof window === "undefined") {
    return () => undefined;
  }

  const onError = (ev: ErrorEvent): void => {
    if (isChunkLoadError(ev.error) || isChunkLoadError(ev.message)) {
      handleChunkLoadFailure();
    }
  };

  const onRejection = (ev: PromiseRejectionEvent): void => {
    if (isChunkLoadError(ev.reason)) {
      handleChunkLoadFailure();
    }
  };

  window.addEventListener("error", onError);
  window.addEventListener("unhandledrejection", onRejection);

  return () => {
    window.removeEventListener("error", onError);
    window.removeEventListener("unhandledrejection", onRejection);
  };
}
