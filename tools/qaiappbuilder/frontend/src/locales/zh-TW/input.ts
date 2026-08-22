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

const input = {
  attachImage: "附加圖片",
  blockedPlaceholder: "另一個對話正在推理中...",
  // 當前標籤頁仍在工作時顯示（回合進行中，或主回合結束後子 Agent / 背景指令
  // 仍在跑）。此時兩個鍵都有用且行為不同，所以提示同時給出：Enter 排隊等安靜後
  // 再送，Ctrl+Enter 插進正在進行的這一輪。CC/OC 對話不用此文案（兩鍵都不支援）。
  busyPlaceholder: "工作中... (Enter 排隊稍後傳送 · Ctrl+Enter 插入目前回合)",
  chars: "{n} 字元",
  imageAttached: "圖片已附加",
  pasteImage: "貼上圖片",
  placeholder: "發訊息... (Enter 傳送 · Shift+Enter 換行)",
  removeImage: "移除圖片",
  streamingPlaceholder: "推理中...",
};

export default input;
