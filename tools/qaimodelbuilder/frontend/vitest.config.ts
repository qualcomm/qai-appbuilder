// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Vitest configuration.
 *
 * Extracted from `vite.config.ts` in S5 PR-050 so the build/dev config
 * stays narrow. Test runner uses `happy-dom` (lighter than jsdom and
 * approved by the new frontend stack).
 */
import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  // Mirror the build-time literal defines from vite.config.ts (§ define,
  // ~L96-99) so components that read the injected `__APP_VERSION__` /
  // `__EDITION__` globals (e.g. AppSidebar.vue) don't throw
  // `ReferenceError: __XXX__ is not defined` under vitest, which has no
  // Vite `define` substitution of its own. Values are test-fixed: the
  // real version/edition are irrelevant to unit tests.
  define: {
    __APP_VERSION__: JSON.stringify("0.0.0-test"),
    __EDITION__: JSON.stringify("internal"),
  },
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    globals: true,
    environment: "happy-dom",
    setupFiles: ["./tests/setup.ts"],
    include: ["src/**/*.spec.ts", "src/**/__tests__/**/*.spec.ts"],
    coverage: {
      provider: "v8",
      reporter: ["text", "html", "lcov"],
      // Route coverage output out of the source tree — data/ is the
      // per-user runtime root (git-ignored). Path is relative to this
      // config file (frontend/), so it resolves to
      // <repo-root>/data/caches/vitest/ at runtime.
      reportsDirectory: "../data/caches/vitest",
      exclude: [
        "node_modules/**",
        "dist/**",
        "js/**",
        "css/**",
        "vendor/**",
        "locales/**",
        "src/types/api.ts",
        "**/*.d.ts",
      ],
    },
  },
});
