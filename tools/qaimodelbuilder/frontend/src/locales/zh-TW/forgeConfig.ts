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

const forgeConfig = {
  copiedToClipboard: "已複製到剪貼簿",
  copyFailed: "複製失敗",
  loadFailed: "載入設定失敗",
  msgCopied: "已複製 #{n} ({role})",
  proxyLoadFailed: "載入代理設定失敗",
  proxySaved: "代理設定已儲存",
  proxySaveFailed: "儲存代理設定失敗",
  remoteModelsFailure: "無法連接遠端服務，模型清單未更新",
  remoteModelsFetched: "已從遠端服務取得 {n} 個模型",
  saveFailed: "儲存失敗",
  saved: "設定已儲存",
  serviceConfigReloaded: "服務設定已重新載入自",
  snapshotLoadFailed: "載入失敗：{msg}",
};

export default forgeConfig;
