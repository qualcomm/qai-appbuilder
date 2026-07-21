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
  advanced: "進階",
  appearance: "外觀",
  dark: "深色",
  general: "一般",
  language: "語言",
  light: "淺色",
  subtitle: {
    agent: "Agent 迴圈調校與各子智能體檔位的模型覆蓋",
    aiCoding: "forge_config.json — AI 程式設計助手設定",
    app: "forge_config.json — QAIModelBuilder 應用程式設定",
    claudeCode: "forge_config.json (claude_code) — Claude Code AI 程式設計助手設定",
    cloudModels: "cloud_models.json — 聊天介面雲端模型目錄",
    codingModes: "「編程」模式下各工作角色的系統提示詞（僅雲端模型生效）",
    hooks: "AI 在聊天事件點自動執行的 shell 命令",
    mcp: "Model Context Protocol 伺服器 — 連接外部工具提供方",
    opencode: "forge_config.json (opencode) — Open Code AI 程式設計助手設定",
  },
  tab: {
    agent: "智能體",
    aiCoding: "AI 程式設計",
    appConfig: "應用設定",
    cloudModels: "雲端模型",
    codingModes: "編程模式",
    hooks: "Hook",
    mcp: "MCP",
  },
  tabAiCoding: "AI 程式設計",
  tabApp: "應用程式",
  tabCloud: "雲端模型",
  theme: "主題",
  title: "設定",
};

export default settings;
