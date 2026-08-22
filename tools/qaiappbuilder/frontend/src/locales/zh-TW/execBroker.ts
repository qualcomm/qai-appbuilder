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
  askAlwaysDesc: "該程式的任何呼叫都會彈窗確認，與參數無關——僅「被使用」本身就構成檔案層無法兜底的非檔案風險（網路、登錄檔、LOLBins）。",
  badgeAsk: "{n} 項詢問",
  badgeAskAlways: "永遠詢問",
  badgeDeny: "{n} 項拒絕",
  badgeIo: "IO 限制",
  badgeNoRules: "無規則",
  colAllowedCommands: "允許的參數",
  colAskArgs: "詢問參數",
  colAskRules: "詢問規則",
  colDeniedPatterns: "硬拒絕",
  colIoConstraints: "IO 限制",
  colSourceSkill: "來源技能",
  loadFailed: "載入執行設定失敗：{msg}",
  noProfiles: "無已載入設定",
  noRulesDesc: "該設定未宣告任何詢問 / 拒絕 / IO 規則——命中的命令將直接執行，不額外攔截。",
  profiles: "已載入設定",
  profilesDesc: "執行代理從磁碟載入的設定。每個設定把命中的命令判定為允許 / 詢問 / 拒絕。唯讀：請編輯 factory/config/exec_profiles/*.toml 後重新載入。",
  reload: "重新載入",
  reloadFailed: "重新載入失敗：{msg}",
  reloadUnavailable: "無法重新載入（未設定設定目錄）",
  reloaded: "已重新載入 {n} 個設定",
};

export default execBroker;
