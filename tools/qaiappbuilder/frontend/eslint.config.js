// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// ESLint flat config — frontend
// S5 PR-050.
//
// Goals:
//  - Vue 3 SFC + TypeScript strict checks.
//  - Forbid hard-coded host:port literals in source (S5 spec §3 forbids
//    `fetch('http://localhost:...')` style); the only allowed file is
//    vite.config.ts which maps proxy targets.
//  - Forbid imports from the legacy `frontend/js/` and `frontend/vendor/`
//    trees so the new Vite app cannot acquire a runtime dependency on
//    code slated for deletion in S8 PR-081.
// =============================================================================

import js from "@eslint/js";
import globals from "globals";
import tsParser from "@typescript-eslint/parser";
import tsPlugin from "@typescript-eslint/eslint-plugin";
import vuePlugin from "eslint-plugin-vue";
import vueParser from "vue-eslint-parser";

const HOST_PORT_FORBIDDEN =
  "Hard-coded host:port literals are forbidden in source per S5 spec §3. Use apiBaseUrl()/wsBaseUrl() and rely on the Vite proxy.";

const LEGACY_PATH_FORBIDDEN =
  "Importing from legacy `frontend/js/` or `frontend/vendor/` is forbidden (refactor-plan §11.3 / S5 spec §3). These trees are deleted in S8 PR-081.";

const sharedRules = {
  "no-console": ["warn", { allow: ["warn", "error"] }],
  "no-debugger": "error",
  "no-restricted-syntax": [
    "error",
    {
      selector:
        "Literal[value=/^(?:https?:\\/\\/(?:127\\.0\\.0\\.1|localhost|0\\.0\\.0\\.0)(?::\\d+)?|wss?:\\/\\/(?:127\\.0\\.0\\.1|localhost|0\\.0\\.0\\.0)(?::\\d+)?)/]",
      message: HOST_PORT_FORBIDDEN,
    },
    {
      selector:
        "TemplateElement[value.cooked=/^(?:https?:\\/\\/(?:127\\.0\\.0\\.1|localhost|0\\.0\\.0\\.0)(?::\\d+)?|wss?:\\/\\/(?:127\\.0\\.0\\.1|localhost|0\\.0\\.0\\.0)(?::\\d+)?)/]",
      message: HOST_PORT_FORBIDDEN,
    },
  ],
  "no-restricted-imports": [
    "error",
    {
      patterns: [
        {
          group: [
            "**/frontend/js/**",
            "**/frontend/vendor/**",
            "../../js/**",
            "../../vendor/**",
            "../js/**",
            "../vendor/**",
          ],
          message: LEGACY_PATH_FORBIDDEN,
        },
      ],
    },
  ],
};

export default [
  {
    ignores: [
      "dist/**",
      "node_modules/**",
      "js/**",
      "css/**",
      "vendor/**",
      "locales/**",
      "public/**",
      "src/types/api.ts",
      "coverage/**",
      "playwright-report/**",
      "test-results/**",
    ],
  },

  js.configs.recommended,

  // TypeScript files
  {
    files: ["**/*.ts", "**/*.tsx"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: 2022,
        sourceType: "module",
      },
      globals: {
        ...globals.browser,
        ...globals.es2022,
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
    },
    rules: {
      ...tsPlugin.configs.recommended.rules,
      ...sharedRules,
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      "@typescript-eslint/consistent-type-imports": [
        "error",
        { prefer: "type-imports" },
      ],
    },
  },

  // Vue SFCs
  ...vuePlugin.configs["flat/recommended"],
  {
    files: ["**/*.vue"],
    languageOptions: {
      parser: vueParser,
      parserOptions: {
        parser: tsParser,
        ecmaVersion: 2022,
        sourceType: "module",
        extraFileExtensions: [".vue"],
      },
      globals: {
        ...globals.browser,
        ...globals.es2022,
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
    },
    rules: {
      ...sharedRules,
      "no-unused-vars": "off",
      "vue/multi-word-component-names": "off",
      "vue/html-self-closing": [
        "error",
        {
          html: { void: "always", normal: "any", component: "always" },
        },
      ],
    },
  },

  // Test files: looser rules for ergonomic mocking.
  {
    files: [
      "**/__tests__/**/*.ts",
      "**/*.spec.ts",
      "**/*.test.ts",
      "tests/**/*.ts",
    ],
    languageOptions: {
      globals: {
        ...globals.node,
      },
    },
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-non-null-assertion": "off",
    },
  },

  // Tooling-side allowance for hard-coded backend addresses.
  {
    files: ["vite.config.ts", "vitest.config.ts"],
    rules: {
      "no-restricted-syntax": "off",
    },
  },

  // Node-side scripts (CommonJS / ESM build helpers and pnpm hooks).
  // These run under Node, not in the browser, so they need Node globals
  // (`module`, `require`, `process`, …) and don't participate in the
  // legacy-import / host:port hardening rules.
  {
    files: ["**/*.cjs", "**/*.mjs", "scripts/**/*.{js,mjs}"],
    languageOptions: {
      globals: {
        ...globals.node,
      },
      sourceType: "module",
    },
    rules: {
      "no-restricted-syntax": "off",
      "no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
];
