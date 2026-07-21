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
  collapseSidebar: "摺疊側邊欄",
  convMoreActions: "更多操作",
  darkTheme: "切換到亮色主題",
  deleteConversation: "刪除",
  earlier: "更早",
  expandSidebar: "展開側邊欄",
  favoriteConversation: "收藏",
  favoritedHint: "已收藏",
  foldItems: "收起",
  fontDecrease: "縮小字型",
  fontIncrease: "放大字型",
  fontReset: "重設為預設大小",
  fontSize: "調整字型大小",
  lightTheme: "切換到暗色主題",
  mainNav: "主導航",
  moreItems: "還有 {n} 條",
  moreTools: "更多",
  newConversation: "新建對話",
  noConversations: "暫無對話記錄",
  noSubAgents: "無子 Agent",
  openFavorites: "我的收藏",
  pinConversation: "置頂",
  reboot: "重新啟動服務",
  recentChats: "最近對話",
  renameConversation: "重新命名",
  rounds: "{n} 輪對話",
  roundsSuffix: "輪",
  subtitle: "AI Agent 平台",
  thisWeek: "本週",
  title: "QAI ModelBuilder",
  titleAppBuilder: "QAI AppBuilder",
  titlePro: "QAI ModelBuilder Pro",
  today: "今天",
  toggleSubAgents: "顯示/隱藏子 Agent",
  toolCalls: "工具呼叫 {n} 次",
  unfavoriteConversation: "取消收藏",
  unpinConversation: "取消置頂",
  yesterday: "昨天",
};

export default sidebar;
