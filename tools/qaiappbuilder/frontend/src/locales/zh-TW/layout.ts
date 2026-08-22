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
  clear: "清除對話",
  collapse_sidebar: "摺疊側欄",
  collapse_tool_cards: "摺疊所有工具卡",
  command_palette_empty: "沒有相符的命令。",
  command_palette_placeholder: "輸入命令或頁面名稱…",
  command_palette_shortcut: "命令面板（Ctrl+K）",
  command_palette_title: "命令面板",
  expand_sidebar: "展開側欄",
  expand_tool_cards: "展開所有工具卡",
  export: "匯出對話",
  header_aria: "應用程式標題列",
  main_aria: "主內容區",
  new_conversation: "新建對話",
  open_command_palette: "開啟命令面板",
  overflowMenu: "更多操作",
  pending_permissions: "待處理權限請求",
  sidebar_aria: "側欄導覽",
  toggle_sidebar: "切換側欄",
  skip_to_content: "跳至主內容",
  switch_to_dark: "切換至深色主題",
  switch_to_light: "切換至淺色主題",
};

export default layout;
