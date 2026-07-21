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
  advanced: "Advanced",
  appearance: "Appearance",
  dark: "Dark",
  general: "General",
  language: "Language",
  light: "Light",
  subtitle: {
    agent: "Agent loop tuning and per-profile sub-agent model overrides",
    aiCoding: "forge_config.json — AI coding assistant settings",
    app: "forge_config.json — QAIModelBuilder application settings",
    claudeCode: "forge_config.json (claude_code) — Claude Code AI coding assistant settings",
    cloudModels: "cloud_models.json — Chat interface cloud model catalog",
    codingModes: "System prompts for the Coding mode personas (cloud models only)",
    hooks: "Shell commands the AI runs automatically at chat event points",
    mcp: "Model Context Protocol servers — connect external tool providers",
    opencode: "forge_config.json (opencode) — Open Code AI coding assistant settings",
  },
  tab: {
    agent: "Agent",
    aiCoding: "AI Coding",
    appConfig: "App Config",
    cloudModels: "Cloud Models",
    codingModes: "Coding Modes",
    hooks: "Hooks",
    mcp: "MCP",
  },
  tabAiCoding: "AI Coding",
  tabApp: "Application",
  tabCloud: "Cloud Models",
  theme: "Theme",
  title: "Settings",
};

export default settings;
