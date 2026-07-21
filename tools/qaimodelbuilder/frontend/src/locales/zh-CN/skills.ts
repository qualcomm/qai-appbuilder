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
  btnBoth: "双模",
  btnBothTitle: "云端 + NPU 同时",
  btnCloud: "云端",
  btnCloudTitle: "云端模型启用",
  btnLocal: "本地",
  btnLocalTitle: "本地 NPU 启用",
  btnOff: "关",
  btnOffTitle: "禁用",
  description: "描述",
  discovered: "已发现 {n} 个",
  enabled: "{n} 个已启用",
  filterAll: "全部",
  filterDisabled: "已禁用",
  filterEnabled: "已启用",
  filterNpu: "NPU",
  loading: "加载技能中...",
  noResults: "没有匹配 “{q}” 的技能",
  npuCount: "🔷 {n} NPU",
  reload: "重新加载",
  useFor: "适用于：",
  mode: "模式",
  modeCloud: "云端",
  modeNpu: "NPU",
  modeOff: "关闭",
  modeResultBoth: "云端 + NPU 同时启用",
  modeResultCloud: "云端启用",
  modeResultLocal: "本地 NPU 启用",
  modeResultOff: "已禁用",
  noSkills: "未找到技能",
  noSkillsHint: "创建 skills/ 目录并添加 SKILL.md 文件即可开始使用。",
  npuDisabledHint: "需要在 SKILL.md 的 tags 行末尾加「.」才能启用本地模型",
  npuOptimized: "NPU 优化",
  reloadFailed: "重新加载失败: ",
  reloaded: "Skills 已重新加载",
  search: "搜索技能...",
  setModeFailed: "设置失败: ",
  statusBoth: "⚡ 双模",
  statusCloud: "☁️ 云端",
  statusLocal: "🔷 本地",
  statusOff: "已禁用",
  title: "技能",
  toggleEnabled: "切换技能状态",
};

export default skills;
