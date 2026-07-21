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

const skills = {
  btnBoth: "雙模",
  btnBothTitle: "雲端 + NPU 同時",
  btnCloud: "雲端",
  btnCloudTitle: "雲端模型啟用",
  btnLocal: "本機",
  btnLocalTitle: "本機 NPU 啟用",
  btnOff: "關",
  btnOffTitle: "停用",
  description: "描述",
  discovered: "已發現 {n} 個",
  enabled: "{n} 個已啟用",
  filterAll: "全部",
  filterDisabled: "已停用",
  filterEnabled: "已啟用",
  filterNpu: "NPU",
  loading: "載入技能中...",
  noResults: "沒有符合 “{q}” 的技能",
  npuCount: "🔷 {n} NPU",
  reload: "重新載入",
  useFor: "適用於：",
  mode: "模式",
  modeCloud: "雲端",
  modeNpu: "NPU",
  modeOff: "關閉",
  modeResultBoth: "雲端 + NPU 同時啟用",
  modeResultCloud: "雲端啟用",
  modeResultLocal: "本機 NPU 啟用",
  modeResultOff: "已停用",
  noSkills: "未找到技能",
  noSkillsHint: "建立 skills/ 目錄並新增 SKILL.md 檔案即可開始使用。",
  npuDisabledHint: "需要在 SKILL.md 的 tags 行末尾加「.」才能啟用本機模型",
  npuOptimized: "NPU 最佳化",
  reloadFailed: "重新載入失敗: ",
  reloaded: "Skills 已重新載入",
  search: "搜尋技能...",
  setModeFailed: "設定失敗: ",
  statusBoth: "⚡ 雙模",
  statusCloud: "☁️ 雲端",
  statusLocal: "🔷 本機",
  statusOff: "已停用",
  title: "技能",
  toggleEnabled: "切換技能狀態",
};

export default skills;
