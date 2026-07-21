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

const tool = {
  cancelExec: "取消工具執行",
  completed: "已完成",
  copyOutput: "複製輸出內容",
  copyResult: "複製結果",
  noParams: "（無參數）",
  output: "輸出",
  params: "參數",
  running: "正在執行",
  truncated: "⚠ 已截斷（模型僅看到摘要）",
  truncatedTitle: "輸出超過 50KB，已向模型發送首尾各 25KB 的摘要版本，中間部分已省略",
  viewFull: "完整輸出",
  viewHead: "開頭 25KB",
  viewPrompt: "查看觸發此工具呼叫的請求發送給模型的完整提示詞",
  viewPromptLabel: "查看完整提示詞",
  viewTail: "結尾 25KB",
  waiting: "等待輸出…",
};

export default tool;
