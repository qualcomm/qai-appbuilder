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

const settings = {
  advanced: "高级",
  appearance: "外观",
  dark: "深色",
  general: "通用",
  language: "语言",
  light: "浅色",
  subtitle: {
    agent: "Agent 循环调优与各子智能体档位的模型覆盖",
    aiCoding: "forge_config.json — AI 编程助手设置",
    app: "forge_config.json — QAIModelBuilder 应用设置",
    claudeCode: "forge_config.json (claude_code) — Claude Code AI 编程助手设置",
    cloudModels: "cloud_models.json — 聊天界面云端模型目录",
    codingModes: "\"编程\"模式下各工作角色的系统提示词（仅云端模型生效）",
    hooks: "AI 在聊天事件点自动执行的 shell 命令",
    mcp: "Model Context Protocol 服务器 — 连接外部工具提供方",
    opencode: "forge_config.json (opencode) — Open Code AI 编程助手设置",
  },
  tab: {
    agent: "智能体",
    aiCoding: "AI 编程",
    appConfig: "应用配置",
    cloudModels: "云端模型",
    codingModes: "编程模式",
    hooks: "Hook",
    mcp: "MCP",
  },
  tabAiCoding: "AI 编程",
  tabApp: "应用",
  tabCloud: "云端模型",
  theme: "主题",
  title: "设置",
};

export default settings;
