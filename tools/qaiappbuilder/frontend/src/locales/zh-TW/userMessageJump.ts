// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工維護，UTF-8（無 BOM）。
//
// 真值源說明：本專案 i18n 已無自動生成管道。本檔就是當前唯一真值源，
// 必須手工維護。修改時嚴守 AGENTS.md §3.10 檔案編碼鐵律（UTF-8，禁止
// GBK/CP437 等非 UTF-8 編碼，禁止雙重編碼損壞）。
//
// 類型：en/{ns}.ts 經主入口 en.ts 組裝後由 typeof 推導出 MessageSchema；
// zh-CN / zh-TW 的同名子檔須保持與 en 完全一致的 key 結構。
// =============================================================================

const userMessageJump = {
  buttonTitle: "跳轉到我發過的訊息",
  title: "跳轉到我的訊息",
  empty: "本對話還沒有發過訊息。",
};

export default userMessageJump;
