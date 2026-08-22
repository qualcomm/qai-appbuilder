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
  btnBoth: "Dual",
  btnBothTitle: "Enable for cloud + NPU",
  btnCloud: "Cloud",
  btnCloudTitle: "Enable for cloud model",
  btnLocal: "Local",
  btnLocalTitle: "Enable for local NPU",
  btnOff: "Off",
  btnOffTitle: "Disable",
  description: "Description",
  discovered: "{n} discovered",
  enabled: "{n} enabled",
  filterAll: "All",
  filterDisabled: "Disabled",
  filterEnabled: "Enabled",
  filterNpu: "NPU",
  loading: "Loading skills...",
  noResults: "No skills match \u201c{q}\u201d",
  npuCount: "🔷 {n} NPU",
  reload: "Reload",
  useFor: "Use for:",
  mode: "Mode",
  modeCloud: "Cloud",
  modeNpu: "NPU",
  modeOff: "Off",
  modeResultBoth: "Cloud + NPU enabled",
  modeResultCloud: "Cloud enabled",
  modeResultLocal: "Local NPU enabled",
  modeResultOff: "Disabled",
  noSkills: "No Skills Found",
  noSkillsHint: "Create a skills/ directory and add SKILL.md files to get started.",
  npuDisabledHint: "Add a \".\" at the end of the tags line in SKILL.md to enable local model",
  npuOptimized: "NPU Optimized",
  reloadFailed: "Reload failed: ",
  reloaded: "Skills reloaded",
  search: "Search Skills...",
  setModeFailed: "Set mode failed: ",
  statusBoth: "⚡ Dual",
  statusCloud: "☁️ Cloud",
  statusLocal: "🔷 Local",
  statusOff: "Disabled",
  title: "Skills",
  toggleEnabled: "Toggle skill",
};

export default skills;
