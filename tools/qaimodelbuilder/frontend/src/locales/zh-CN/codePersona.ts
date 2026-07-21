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

const codePersona = {
  activeBadge: "当前",
  alreadyDefaultHint: "当前已是默认值",
  architect: {
    desc: "在动手编码前先做好拆解与设计",
    name: "方案规划",
  },
  ask: {
    desc: "讲解概念、分析代码、给出建议",
    name: "答疑解释",
  },
  cancel: "取消",
  code: {
    desc: "编写、修改与重构代码",
    name: "编码实现",
  },
  createFailed: "创建角色失败",
  customizedHint: "已自定义（点击可编辑）",
  customizedTag: "已自定义",
  deleteFailed: "删除模式失败",
  deleteNotSupported: "服务端不支持删除",
  debugger: {
    desc: "系统化地定位并修复问题",
    name: "排错诊断",
  },
  discardConfirm: "当前有未保存的修改，确定放弃吗？",
  editPrompts: "编辑提示词…",
  groups: {
    command: "命令执行",
    commandDesc: "运行 Shell 命令和后台进程",
    edit: "文件编辑",
    editDesc: "创建、修改和补丁文件",
    label: "工具权限",
    read: "文件读取",
    readDesc: "读取文件、搜索内容、浏览网页",
    resetGroups: "重置权限",
    restrictedHint: "限制范围：{pattern}",
    other: "其它",
  },
  loadFailed: "加载编程模式失败",
  loading: "正在加载…",
  menuTitle: "编程模式",
  noModesFound: "未找到编程模式。",
  orchestrator: {
    desc: "把大任务拆成可独立完成的子任务",
    name: "任务协调",
  },
  optimizer: {
    desc: "重构与优化代码，提升可读性与性能",
    name: "重构优化",
  },
  promptLabel: "系统提示词",
  promptPlaceholder: "输入该模式下使用的系统提示词。留空或点击\"恢复默认\"即可还原。",
  reload: "重新加载",
  reloadHint: "从服务端重新加载（丢弃本地草稿）",
  removed: "模式已删除",
  removeBtn: "删除",
  removeTitle: "删除此模式",
  resetConfirm: "确定将\"{name}\"恢复为默认提示词吗？你的自定义内容将被清除。",
  resetFailed: "恢复失败",
  resetSuccess: "\"{name}\" 已恢复默认",
  resetToDefault: "恢复默认",
  reviewer: {
    desc: "审查代码的正确性、风格与可维护性",
    name: "代码审查",
  },
  save: "保存",
  saveFailed: "保存失败",
  saveSuccess: "\"{name}\" 已保存",
  selectFailed: "选择角色失败",
  settingsDesc: "为不同编程模式自定义系统提示词。仅在使用云端模型且选中\"编程\"功能时生效。修改按模式保存，重启不丢失。",
  settingsTitle: "编程模式提示词",
  switchFailed: "切换编程模式失败",
  unsavedChanges: "存在未保存的修改",
};

export default codePersona;
