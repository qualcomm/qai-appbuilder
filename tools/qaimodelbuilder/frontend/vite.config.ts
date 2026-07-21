// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// Vite configuration — QAIModelBuilder new frontend
// Created in S1 PR-011, definitively shaped in S5 PR-050.
//
// Notes:
//  - The Vite SPA entry is frontend/index.html (renamed from
//    index-new.html in PR-1103).
//  - vitest config has been extracted to vitest.config.ts (PR-050) so this
//    file remains the single source of truth for build/dev concerns only.
// =============================================================================

import { readFileSync } from "node:fs";
import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath, URL } from "node:url";

const pkg = JSON.parse(
  readFileSync(fileURLToPath(new URL("./package.json", import.meta.url)), "utf-8"),
);

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, fileURLToPath(new URL(".", import.meta.url)), [
    "VITE_",
    "QAI_",
  ]);

  // Backend dev target. Default 8900 matches V2 backend and
  // api-contract.md §6.4. Override via QAI_DEV_BACKEND_HTTP / _WS in
  // .env.local for non-default backends; never hard-code other hosts in
  // application source.
  const backendHttp = env.QAI_DEV_BACKEND_HTTP ?? "http://127.0.0.1:8900";
  const backendWs = env.QAI_DEV_BACKEND_WS ?? "ws://127.0.0.1:8900";

  // -------------------------------------------------------------------------
  // Edition split (build-time). QAI_EDITION selects which feature set is
  // compiled into the bundle. The release pipeline (scripts/release/build.py)
  // sets QAI_EDITION=external so internal-only feature code is dead-code-
  // eliminated and physically absent from the external open-source bundle.
  // Defaults to "internal" for dev / `pnpm build` without the flag.
  //   - `__EDITION__` is substituted as a literal so `IS_INTERNAL` guards
  //     collapse to `if (false)` on external builds → Rollup drops the
  //     guarded dynamic import() and the internal chunk is never emitted.
  //   - The external-only resolve aliases below map any residual internal
  //     module specifier to an empty stub so a physically-removed internal
  //     source tree still resolves and nothing internal can leak into the
  //     external bundle (defence-in-depth).
  // -------------------------------------------------------------------------
  const edition = env.QAI_EDITION === "external" ? "external" : "internal";
  const isExternal = edition === "external";
  const emptyModule = fileURLToPath(
    new URL("./src/edition-stubs/empty.ts", import.meta.url),
  );
  const emptyComponent = fileURLToPath(
    new URL("./src/edition-stubs/EmptyComponent.vue", import.meta.url),
  );
  const internalAliases = isExternal
    ? [
        // Any internal-only .vue component → empty render component.
        {
          find: /^@\/components\/gomaster\/.*\.vue$/,
          replacement: emptyComponent,
        },
        {
          find: "@/components/chat/ProSettingsDialog.vue",
          replacement: emptyComponent,
        },
        {
          find: "@/components/chat/toolbar-modes/ModeFramePro.vue",
          replacement: emptyComponent,
        },
        {
          find: "@/components/chat/toolbar-modes/ModeFrameGomaster.vue",
          replacement: emptyComponent,
        },
        // Internal-only composables / modules → inert module stub.
        { find: "@/composables/useGomasterConnection", replacement: emptyModule },
        { find: "@/composables/useGomasterOptimize", replacement: emptyModule },
        { find: "@/composables/useProConnection", replacement: emptyModule },
      ]
    : [];

  return {
    root: fileURLToPath(new URL(".", import.meta.url)),
    base: "/",

    // Route Vite's dep-optimize cache out of the source tree — data/ is the
    // per-user runtime root (git-ignored). Keeps frontend/ clean of
    // frontend/.vite/ scratch dirs. Path is relative to this config file
    // (frontend/), so it resolves to <repo-root>/data/caches/vite/ at runtime.
    cacheDir: "../data/caches/vite",

    define: {
      __APP_VERSION__: JSON.stringify(pkg.version as string),
      __EDITION__: JSON.stringify(edition),
    },

    plugins: [vue()],

    resolve: {
      // Alias array form: internal-only entries first (most specific), then
      // the generic "@" prefix. External builds redirect internal specifiers
      // to inert stubs; internal builds get an empty internal-alias list.
      alias: [
        ...internalAliases,
        { find: "@", replacement: fileURLToPath(new URL("./src", import.meta.url)) },
      ],
    },

    // Use a non-conflicting dev entry so legacy index.html keeps working
    // until S8 PR-081. Build output also temporarily lands under
    // frontend/dist/.
    build: {
      outDir: "dist",
      emptyOutDir: true,
      sourcemap: mode !== "production",
      target: "es2022",
      rollupOptions: {
        input: fileURLToPath(new URL("./index.html", import.meta.url)),
        output: {
          manualChunks(id: string) {
            // -------------------------------------------------------------
            // Locale chunking strategy (S7.5 L8 PR-803; i18n 重构后更新).
            //
            // i18n 重构后，每种语言的所有命名空间都各自拆成一个子文件
            // (frontend/src/locales/{lang}/{ns}.ts)，主 `{lang}.ts` 只做纯组装。
            // 这里给每个 (lang, ns) 子文件单独成 chunk，使每个 locale chunk 都
            // 远低于 256 KB 上限 (PR-803 P0-FR10)；主 `{lang}.ts` 仅含 import +
            // 组装，落到 `locale-{lang}` 这个很小的 chunk。
            //
            // 命名规范：子文件 → `locale-{lang}-{ns}`，主入口 → `locale-{lang}`。
            // 正则用 `[^/]+` 通配 ns，自动覆盖未来新增的命名空间，无需改此处。
            // -------------------------------------------------------------
            const localeSubMatch = id.match(
              /\/src\/locales\/(en|zh-CN|zh-TW)\/([^/]+)\.ts$/,
            );
            if (localeSubMatch) {
              const [, lang, ns] = localeSubMatch;
              return `locale-${lang}-${ns}`;
            }
            // Match `frontend/src/locales/{lang}.ts` (the main wrapper).
            const localeMainMatch = id.match(
              /\/src\/locales\/(en|zh-CN|zh-TW)\.ts$/,
            );
            if (localeMainMatch) {
              return `locale-${localeMainMatch[1]}`;
            }

            // --- node_modules vendor splits ---
            if (!id.includes("node_modules")) return undefined;

            if (
              id.includes("/vue/") ||
              id.includes("/vue-router/") ||
              id.includes("/pinia/") ||
              id.includes("/vue-i18n/") ||
              id.includes("/@vueuse/") ||
              id.includes("/@vue/")
            ) {
              return "vendor-vue";
            }
            if (
              id.includes("/marked/") ||
              id.includes("/dompurify/") ||
              id.includes("/highlight.js/")
            ) {
              return "vendor-markdown";
            }
            return undefined;
          },
        },
      },
      // PR-056: lowered from 600 → 400 as bundle-split constraint.
      // PR-803: 156 KB per locale chunk is the P0-FR10 hard cap; rollup
      // warning at 400 KB is informational only (every locale chunk now
      // is <= 35 KB after the per-namespace split).
      chunkSizeWarningLimit: 400,
    },

    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      // Proxy contract surfaces to the dev backend. Path prefixes are
      // sourced from api-contract.md §1.1 (route prefixes) and §4 (WS).
      proxy: {
        "/api": {
          target: backendHttp,
          changeOrigin: true,
        },
        "/v1": {
          target: backendHttp,
          changeOrigin: true,
        },
        "/api/chat/ws": {
          target: backendWs,
          ws: true,
          changeOrigin: true,
        },
        // Sub-agent live progress WS (block 2; migrated from SSE so concurrent
        // sub-agent tabs don't exhaust the browser's ~6 per-host HTTP/1.1
        // connections). Regex key so ONLY the `…/subagents/{id}/ws` upgrade is
        // WS-proxied — the sibling HTTP `…/subagents/{id}` GET + `/stream`
        // (legacy SSE) still go through the plain `/api` proxy above.
        "^/api/chat/subagents/[^/]+/ws$": {
          target: backendWs,
          ws: true,
          changeOrigin: true,
        },
      },
    },

    preview: {
      host: "127.0.0.1",
      port: 4173,
      strictPort: true,
    },
  };
});
