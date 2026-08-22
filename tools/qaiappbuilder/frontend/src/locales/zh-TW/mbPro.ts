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
// 類型：en/{ns}.ts 經主入口 en.ts 組裝後由 typeof 推導出 MessageSchema；
// zh-CN / zh-TW 的同名子檔案須保持與 en 完全一致的 key 結構（由 locale
// parity 測試 + tsc 強制）。
//
// mbPro：Model Builder Pro 聊天卡片文案。configReview = MB Pro 的
// "配置確認"卡（ConfigReviewCard.vue，映射自 upstream 的
// config_review_needed 事件）。
// =============================================================================

const mbPro = {
  configReview: {
    title: "配置確認",
    countdownLabel: "剩餘確認時間",
    platform: "平台",
    model: "模型",
    userConstraint: "使用者約束",
    paths: "路徑",
    params: "參數",
    inputPaths: "執行時輸入路徑",
    notebook: "Notebook",
    hint: "在下方輸入「確認」開跑，或直接說明要改的參數。",
    hintExpired: "倒數結束，如未回應系統將自動開跑；如需修改請在下方輸入。",
  },
};

export default mbPro;
