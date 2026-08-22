// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工維護，UTF-8（無 BOM）。
//
// 真值源說明：本專案 i18n 已無自動生成管道（舊的 _L8-locale-gen.py 與
// _migrated/*.json 均未保留在倉庫）。因此本檔案就是目前唯一真值源，
// 必須手工維護。修改時嚴守 AGENTS.md §3.10 檔案編碼鐵律（UTF-8，禁止
// GBK/CP437 等非 UTF-8 編碼，禁止雙重編碼損壞）。
//
// 型別：en/{ns}.ts 經主入口 en.ts 組裝後由 typeof 推導出 MessageSchema；
// zh-CN / zh-TW 的同名子檔案須保持與 en 完全一致的 key 結構（由 locale
// parity 測試 + tsc 強制）。
// =============================================================================

// QAI Service 令牌池餘額 —— 側邊欄用量條。"Bonus Token" 沿用服務端自己的
// 叫法不作翻譯，這樣使用者看到的名稱與管理員在服務側看到的一致。
const quota = {
  label: "Bonus Token",
  resetsAt: "{when} 重置",
  tooltip: "Bonus Token 餘額：剩餘 {remaining} / {allocated}",
};

export default quota;
