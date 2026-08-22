// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。
//
// 真值源说明：本项目 i18n 已无自动生成管道（旧的 _L8-locale-gen.py 与
// _migrated/*.json 均未保留在仓库）。因此本文件就是当前唯一真值源，
// 必须手工维护。修改时严守 AGENTS.md §3.10 文件编码铁律（UTF-8，禁止
// GBK/CP437 等非 UTF-8 编码，禁止双重编码损坏）。
//
// 类型：en/{ns}.ts 经主入口 en.ts 组装后由 typeof 推导出 MessageSchema；
// zh-CN / zh-TW 的同名子文件须保持与 en 完全一致的 key 结构（由 locale
// parity 测试 + tsc 强制）。
// =============================================================================

const policyTemplates = {
  applied: "Template applied successfully",
  apply: "Apply",
  applyFailed: "Failed to apply template",
  current: "Current",
  desc: "Select a policy template to quickly apply a predefined security configuration.",
  description: "Apply a pre-defined security policy template.",
  empty: "No policy templates available.",
  title: "Policy Templates",
  clearTitle: "Clear all rules",
  clearHint:
    "Remove every rule so the ambient allow cascade (workspace / skill / auto-approve / file_guard_paths / grants) drives decisions; anything not covered by those falls through to the authorization dialog.",
  clearBtn: "Clear rules",
  // Per-template display text keyed by the built-in template id. Rendered by
  // id lookup with a fallback to the backend-provided name/description, so a
  // future template the UI has not translated yet still shows (untranslated)
  // rather than blank.
  items: {
    demo: {
      name: "Demo",
      description:
        "Read-only mode with no dynamic authorization popups. AI can only read files. Best for demos and presentations.",
    },
    development: {
      name: "Development",
      description:
        "Project files can be read without confirmation. Write and execute operations still require approval. Good for active development.",
    },
    strict: {
      name: "Strict",
      description:
        "All file and command operations require explicit user approval. Best for high-security environments.",
    },
  },
};

export default policyTemplates;
