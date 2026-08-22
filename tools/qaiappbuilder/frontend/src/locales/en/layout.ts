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

const layout = {
  clear: "Clear conversation",
  collapse_sidebar: "Collapse sidebar",
  collapse_tool_cards: "Collapse all tool call cards",
  command_palette_empty: "No matching commands.",
  command_palette_placeholder: "Type a command or page…",
  command_palette_shortcut: "Command palette (Ctrl+K)",
  command_palette_title: "Command palette",
  expand_sidebar: "Expand sidebar",
  expand_tool_cards: "Expand all tool call cards",
  export: "Export conversation",
  header_aria: "Application header",
  main_aria: "Main content",
  new_conversation: "New conversation",
  open_command_palette: "Open command palette",
  overflowMenu: "More actions",
  pending_permissions: "Pending permission requests",
  sidebar_aria: "Sidebar navigation",
  toggle_sidebar: "Toggle sidebar",
  skip_to_content: "Skip to main content",
  switch_to_dark: "Switch to dark theme",
  switch_to_light: "Switch to light theme",
};

export default layout;
