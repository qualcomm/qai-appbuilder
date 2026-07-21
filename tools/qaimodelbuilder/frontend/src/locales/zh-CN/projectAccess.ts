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

const projectAccess = {
  confirmDisable: "关闭项目访问后，AI 将无法读取或修改项目文件。是否继续？",
  confirmEnable: "允许 AI 访问项目目录：{path}？",
  description: "控制 AI 是否可以读取和修改项目目录中的文件。",
  dialogs: {
    disableTitle: "关闭项目访问",
    enableTitle: "启用项目访问",
  },
  disableLabel: "AI 项目目录访问已关闭",
  disabledWarning: "项目目录访问已关闭。AI 无法读取或修改项目文件。",
  enableHint: "开启后，AI 可以读取、搜索和修改配置的项目路径中的文件。",
  enableLabel: "允许 AI 访问项目目录",
  notifications: {
    saveFailed: "保存项目访问设置失败",
    saved: "项目访问设置已保存",
  },
  pathHint: "项目目录的绝对路径（例如 C:\\Users\\you\\MyProject）",
  pathLabel: "项目目录路径",
  pathPlaceholder: "C:\\Users\\you\\MyProject",
  resetSkipDirs: "恢复默认",
  skipDirPlaceholder: "添加目录名...",
  skipDirsEmpty: "未配置任何目录",
  skipDirsHint: "glob/grep 工具自动跳过的目录名（如 venv、node_modules），可提高性能并减少无关结果。",
  skipDirsLabel: "跳过的目录",
  status: {
    disabled: "项目访问已关闭",
    enabled: "项目访问已启用",
  },
  title: "项目目录访问",
};

export default projectAccess;
