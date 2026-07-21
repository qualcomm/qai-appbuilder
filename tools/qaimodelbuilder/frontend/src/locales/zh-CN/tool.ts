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
  cancelExec: "取消工具执行",
  completed: "已完成",
  copyOutput: "复制输出内容",
  copyResult: "复制结果",
  noParams: "（无参数）",
  output: "输出",
  params: "参数",
  running: "正在运行",
  truncated: "⚠ 已截断（模型仅看到摘要）",
  truncatedTitle: "输出超过 50KB，已向模型发送首尾各 25KB 的摘要版本，中间部分已省略",
  viewFull: "完整输出",
  viewHead: "开头 25KB",
  viewPrompt: "查看触发此工具调用的请求发送给模型的完整提示词",
  viewPromptLabel: "查看完整提示词",
  viewTail: "结尾 25KB",
  waiting: "等待输出…",
};

export default tool;
