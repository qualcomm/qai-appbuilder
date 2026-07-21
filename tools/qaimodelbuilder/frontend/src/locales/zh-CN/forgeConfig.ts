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
  copiedToClipboard: "已复制到剪贴板",
  copyFailed: "复制失败",
  loadFailed: "加载配置失败",
  msgCopied: "已复制 #{n} ({role})",
  proxyLoadFailed: "加载代理设置失败",
  proxySaved: "代理设置已保存",
  proxySaveFailed: "保存代理设置失败",
  remoteModelsFailure: "无法连接远程服务，模型列表未更新",
  remoteModelsFetched: "已从远程服务获取 {n} 个模型",
  saveFailed: "保存失败",
  saved: "设置已保存",
  serviceConfigReloaded: "服务配置已重新加载自",
  snapshotLoadFailed: "加载失败：{msg}",
};

export default forgeConfig;
