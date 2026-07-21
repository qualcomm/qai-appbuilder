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

const error = {
  fileTooLarge: "檔案過大。",
  invalidInput: "輸入無效，請檢查您的資料。",
  network: "網路錯誤，請檢查連線。",
  notFound: "找不到資源。",
  rateLimit: "請求過於頻繁，請稍等。",
  retry: "重試",
  serverError: "伺服器錯誤，請稍後重試。",
  timeout: "請求逾時，請重試。",
  title: "發生問題",
  unauthorized: "未授權，請檢查您的認證資訊。",
  unknown: "發生未知錯誤。",
  unsupportedFormat: "不支援的檔案格式。",
};

export default error;
