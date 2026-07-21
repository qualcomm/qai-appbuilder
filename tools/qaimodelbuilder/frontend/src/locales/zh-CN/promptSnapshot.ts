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

const promptSnapshot = {
  collapseAll: "折叠全部",
  expandAll: "展开全部",
  hideRaw: "收起原始",
  paramOptional: "可选参数",
  paramRequired: "必填参数",
  requestOptions: "请求参数",
  requestOptionsHint: "实际发送的 tools / tool_choice / 采样参数 / session_id",
  samplingParams: "采样参数",
  showRaw: "原始",
  systemPrompt: "系统提示词",
  title: "完整提示词",
  tokens: "tokens",
  toolResults: "工具结果",
  toolsSent: "发送的工具（{n} 个，按发送顺序）",
  userMessage: "用户消息",
};

export default promptSnapshot;
