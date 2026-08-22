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
  askAlwaysDesc: "Every invocation of this program prompts for confirmation, regardless of arguments — its mere use is a non-file risk (network, registry, LOLBins) the file layer cannot backstop.",
  badgeAsk: "{n} ask",
  badgeAskAlways: "always ask",
  badgeDeny: "{n} deny",
  badgeIo: "IO limits",
  badgeNoRules: "no rules",
  colAllowedCommands: "Allowed Args",
  colAskArgs: "Ask Flags",
  colAskRules: "Ask Rules",
  colDeniedPatterns: "Hard Deny",
  colIoConstraints: "IO Limits",
  colSourceSkill: "Source Skill",
  loadFailed: "Failed to load exec profiles: {msg}",
  noProfiles: "No profiles loaded",
  noRulesDesc: "This profile declares no ask / deny / IO rules — a matching command runs without extra gating.",
  profiles: "Loaded Profiles",
  profilesDesc: "Profiles the exec broker loaded from disk. Each one classifies a matching command as allow / ask / deny. Read-only: edit factory/config/exec_profiles/*.toml and reload.",
  reload: "Reload",
  reloadFailed: "Reload failed: {msg}",
  reloadUnavailable: "Reload unavailable (no profile directory configured)",
  reloaded: "Reloaded {n} profiles",
};

export default execBroker;
