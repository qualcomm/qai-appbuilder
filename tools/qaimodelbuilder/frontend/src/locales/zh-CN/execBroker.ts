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

const execBroker = {
  colAllowedCommands: "允许的命令",
  colDeniedArgs: "禁止的参数",
  colDeniedPatterns: "禁止的模式",
  colMatchPattern: "匹配模式",
  colName: "名称",
  deniedArgs: "禁止的参数",
  description: "基于 Profile 的执行约束引擎，限制已知工具的参数。",
  enabled: "启用执行代理",
  matchGlob: "匹配模式",
  noProfiles: "无已加载配置",
  profileName: "配置名称",
  profiles: "已加载配置",
  profilesDesc: "已加载的执行配置文件，控制可运行的程序和禁止的参数。",
  title: "执行配置文件",
};

export default execBroker;
