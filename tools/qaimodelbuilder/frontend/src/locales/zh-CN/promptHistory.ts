// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。
//
// 真值源说明：本项目 i18n 已无自动生成管道。本文件就是当前唯一真值源，
// 必须手工维护。修改时严守 AGENTS.md §3.10 文件编码铁律（UTF-8，禁止
// GBK/CP437 等非 UTF-8 编码，禁止双重编码损坏）。
//
// 类型：en/{ns}.ts 经主入口 en.ts 组装后由 typeof 推导出 MessageSchema；
// zh-CN / zh-TW 的同名子文件须保持与 en 完全一致的 key 结构。
// =============================================================================

const promptHistory = {
  buttonTitle: "历史 prompt 与收藏",
  title: "Prompt",
  searchPlaceholder: "搜索 prompt...",
  favorites: "收藏",
  recent: "最近",
  favEmpty: "在下方点 ⭐ 收藏常用 prompt，方便随时取用。",
  empty: "还没有 prompt。你发送过的内容会显示在这里。",
  noResults: "没有匹配的 prompt。",
  fav: "加入收藏",
  unfav: "取消收藏",
  removeRecent: "从历史中移除",
  clear: "清空历史",
  clearTitle: "清空 prompt 历史？",
  clearConfirm: "这会移除全部最近 prompt，你的收藏会保留。",
  fillTitle: "点击填入输入框",
  showAllFavorites: "显示全部收藏",
  showFewerFavorites: "收起",
};

export default promptHistory;
