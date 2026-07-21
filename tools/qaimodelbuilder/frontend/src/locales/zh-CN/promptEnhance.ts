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

const promptEnhance = {
  aria: {
    label: "增强提示词",
    undo: "撤销提示词增强",
  },
  toast: {
    empty: "请先输入内容",
    failed: "提示增强失败：{msg}",
    success: "已增强提示（{sec}s）",
    undone: "已恢复原始提示词",
  },
  tooltip: {
    empty: "输入提示词后可增强",
    enhancing: "正在增强提示词…",
    idle: "增强提示词",
    undo: "点击撤销，恢复原始提示词",
  },
};

export default promptEnhance;
