// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工維護，UTF-8（無 BOM）。
//
// 真值源說明：本專案 i18n 已無自動生成管道。本檔案就是當前唯一真值源，
// 必須手工維護。修改時嚴守 AGENTS.md §3.10 檔案編碼鐵律（UTF-8，禁止
// GBK/CP437 等非 UTF-8 編碼，禁止雙重編碼損壞）。
//
// 類型：en/{ns}.ts 經主入口 en.ts 組裝後由 typeof 推導出 MessageSchema；
// zh-CN / zh-TW 的同名子檔案須保持與 en 完全一致的 key 結構。
// =============================================================================

const promptHistory = {
  buttonTitle: "歷史 prompt 與收藏",
  title: "Prompt",
  searchPlaceholder: "搜尋 prompt...",
  favorites: "收藏",
  recent: "最近",
  favEmpty: "在下方點 ⭐ 收藏常用 prompt，方便隨時取用。",
  empty: "還沒有 prompt。你傳送過的內容會顯示在這裡。",
  noResults: "沒有符合的 prompt。",
  fav: "加入收藏",
  unfav: "取消收藏",
  removeRecent: "從歷史中移除",
  clear: "清空歷史",
  clearTitle: "清空 prompt 歷史？",
  clearConfirm: "這會移除全部最近 prompt，你的收藏會保留。",
  fillTitle: "點擊填入輸入框",
  showAllFavorites: "顯示全部收藏",
  showFewerFavorites: "收起",
};

export default promptHistory;
