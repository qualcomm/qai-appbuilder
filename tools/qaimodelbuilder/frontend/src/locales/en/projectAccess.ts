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

const projectAccess = {
  confirmDisable: "Disabling project access will prevent the AI from reading or modifying project files. Continue?",
  confirmEnable: "Enable AI access to the project directory at: {path}?",
  description: "Control whether the AI can read and modify files in your project directory.",
  dialogs: {
    disableTitle: "Disable Project Access",
    enableTitle: "Enable Project Access",
  },
  disableLabel: "AI project directory access is disabled",
  disabledWarning: "Project directory access is disabled. The AI cannot read or modify project files.",
  enableHint: "When enabled, the AI can read, search, and modify files in the configured project path.",
  enableLabel: "Allow AI to access project directory",
  notifications: {
    saveFailed: "Failed to save project access settings",
    saved: "Project access settings saved",
  },
  pathHint: "Absolute path to your project directory (e.g. C:\\Users\\you\\MyProject)",
  pathLabel: "Project directory path",
  pathPlaceholder: "C:\\Users\\you\\MyProject",
  status: {
    disabled: "Project access disabled",
    enabled: "Project access enabled",
  },
  title: "Project Directory Access",
};

export default projectAccess;
