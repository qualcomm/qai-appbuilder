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

const auth = {
  account_menu: "帳戶選單",
  contact_support: "聯絡支援",
  prompt_hint: "您將被跳轉到 Qualcomm 登入頁面。",
  prompt_message: "請使用您的 Qualcomm 帳號登入後繼續使用 QAI AppBuilder。",
  prompt_title: "需要登入",
  redirecting: "正在跳轉…",
  sign_in: "登入",
  sign_out: "登出",
  sign_out_confirm: "確定要登出嗎？",
  signed_in: "已登入",
  welcome_back: "歡迎回來，{name}",
};

export default auth;
