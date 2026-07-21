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
  collapseSidebar: "折叠侧栏",
  convMoreActions: "更多操作",
  darkTheme: "切换到亮色主题",
  deleteConversation: "删除",
  earlier: "更早",
  expandSidebar: "展开侧栏",
  favoriteConversation: "收藏",
  favoritedHint: "已收藏",
  foldItems: "收起",
  fontDecrease: "缩小字体",
  fontIncrease: "放大字体",
  fontReset: "重置为默认大小",
  fontSize: "调整字体大小",
  lightTheme: "切换到暗色主题",
  mainNav: "主导航",
  moreItems: "还有 {n} 条",
  moreTools: "更多",
  newConversation: "新建对话",
  noConversations: "暂无对话记录",
  noSubAgents: "无子 Agent",
  openFavorites: "我的收藏",
  pinConversation: "置顶",
  reboot: "重启服务",
  recentChats: "最近对话",
  renameConversation: "重命名",
  rounds: "{n} 轮对话",
  roundsSuffix: "轮",
  subtitle: "AI Agent 平台",
  thisWeek: "本周",
  title: "QAI ModelBuilder",
  titleAppBuilder: "QAI AppBuilder",
  titlePro: "QAI ModelBuilder Pro",
  today: "今天",
  toggleSubAgents: "显示/隐藏子 Agent",
  toolCalls: "工具调用 {n} 次",
  unfavoriteConversation: "取消收藏",
  unpinConversation: "取消置顶",
  yesterday: "昨天",
};

export default sidebar;
