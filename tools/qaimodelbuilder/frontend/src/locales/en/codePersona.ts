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

const codePersona = {
  activeBadge: "Active",
  alreadyDefaultHint: "Already at default",
  architect: {
    desc: "Plan and design before writing code",
    name: "Architect",
  },
  ask: {
    desc: "Explain concepts, analyze code, give advice",
    name: "Ask",
  },
  cancel: "Cancel",
  code: {
    desc: "Write, modify, and refactor code",
    name: "Code",
  },
  createFailed: "Failed to create persona",
  customizedHint: "Customized (click to edit)",
  customizedTag: "Customized",
  deleteFailed: "Failed to delete persona",
  deleteNotSupported: "Delete not supported by server",
  debugger: {
    desc: "Diagnose issues systematically",
    name: "Debug",
  },
  discardConfirm: "You have unsaved changes. Discard them?",
  editPrompts: "Edit prompts...",
  groups: {
    command: "Command execution",
    commandDesc: "Run shell commands and background processes",
    edit: "File editing",
    editDesc: "Create, modify, and patch files",
    label: "Tool permissions",
    read: "File reading",
    readDesc: "Read files, search content, browse web",
    resetGroups: "Reset permissions",
    restrictedHint: "Restricted to: {pattern}",
    other: "Other",
  },
  loadFailed: "Failed to load coding personas",
  loading: "Loading personas...",
  menuTitle: "Coding Mode",
  noModesFound: "No coding modes found.",
  orchestrator: {
    desc: "Break large tasks into independent subtasks",
    name: "Orchestrator",
  },
  optimizer: {
    desc: "Refactor and optimize code for clarity and performance",
    name: "Optimizer",
  },
  promptLabel: "System prompt",
  promptPlaceholder: "Enter the system prompt for this mode. Leave empty or click \"Reset to default\" to restore.",
  reload: "Reload",
  reloadHint: "Reload from server (discard local edits)",
  removed: "Persona removed",
  removeBtn: "Remove",
  removeTitle: "Remove this persona",
  resetConfirm: "Reset \"{name}\" to the default prompt? Your custom prompt will be lost.",
  resetFailed: "Reset failed",
  resetSuccess: "\"{name}\" reset to default",
  resetToDefault: "Reset to default",
  reviewer: {
    desc: "Review code for correctness, style, and maintainability",
    name: "Reviewer",
  },
  save: "Save",
  saveFailed: "Save failed",
  saveSuccess: "\"{name}\" saved",
  selectFailed: "Failed to select persona",
  settingsDesc: "Customize the system prompt for each coding mode. Only effective for cloud models in Coding mode. Edits are saved per-mode and persist across restarts.",
  settingsTitle: "Coding Mode Prompts",
  switchFailed: "Failed to switch coding mode",
  unsavedChanges: "You have unsaved changes",
};

export default codePersona;
