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

const sidebar = {
  collapseSidebar: "Collapse sidebar",
  convMoreActions: "More actions",
  darkTheme: "Switch to light theme",
  deleteConversation: "Delete",
  earlier: "Earlier",
  expandSidebar: "Expand sidebar",
  favoriteConversation: "Favorite",
  favoritedHint: "Favorited",
  foldItems: "Collapse",
  fontDecrease: "Decrease font",
  fontIncrease: "Increase font",
  fontReset: "Reset to default size",
  fontSize: "Adjust font size",
  lightTheme: "Switch to dark theme",
  mainNav: "Main navigation",
  moreItems: "{n} more",
  moreTools: "More",
  newConversation: "New conversation",
  noConversations: "No conversations yet",
  noSubAgents: "No sub-agents",
  openFavorites: "My Favorites",
  pinConversation: "Pin",
  reboot: "Restart service",
  recentChats: "Recent Chats",
  renameConversation: "Rename",
  rounds: "{n} rounds",
  roundsSuffix: "rounds",
  subtitle: "AI Agent Platform",
  thisWeek: "This Week",
  title: "QAI ModelBuilder",
  titleAppBuilder: "QAI AppBuilder",
  titlePro: "QAI ModelBuilder Pro",
  today: "Today",
  toggleSubAgents: "Show/hide sub-agents",
  toolCalls: "Tool calls: {n}",
  unfavoriteConversation: "Remove from favorites",
  unpinConversation: "Unpin",
  yesterday: "Yesterday",
};

export default sidebar;
