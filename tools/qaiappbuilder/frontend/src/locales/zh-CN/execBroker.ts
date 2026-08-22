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
  askAlwaysDesc: "该程序的任何调用都会弹窗确认，与参数无关——仅「被使用」本身就构成文件层无法兜底的非文件风险（网络、注册表、LOLBins）。",
  badgeAsk: "{n} 项询问",
  badgeAskAlways: "始终询问",
  badgeDeny: "{n} 项拒绝",
  badgeIo: "IO 限制",
  badgeNoRules: "无规则",
  colAllowedCommands: "允许的参数",
  colAskArgs: "询问参数",
  colAskRules: "询问规则",
  colDeniedPatterns: "硬拒绝",
  colIoConstraints: "IO 限制",
  colSourceSkill: "来源技能",
  loadFailed: "加载执行配置失败：{msg}",
  noProfiles: "无已加载配置",
  noRulesDesc: "该配置未声明任何询问 / 拒绝 / IO 规则——命中的命令将直接执行，不额外拦截。",
  profiles: "已加载配置",
  profilesDesc: "执行代理从磁盘加载的配置。每个配置把命中的命令判定为允许 / 询问 / 拒绝。只读：请编辑 factory/config/exec_profiles/*.toml 后重新加载。",
  reload: "重新加载",
  reloadFailed: "重新加载失败：{msg}",
  reloadUnavailable: "无法重新加载（未配置配置目录）",
  reloaded: "已重新加载 {n} 个配置",
};

export default execBroker;
