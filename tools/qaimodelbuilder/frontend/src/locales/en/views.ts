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

const views = {
  channels: {
    description: "Manage messaging channels and connect external platforms",
    title: "Channels",
  },
  chat: {
    description: "Conversational AI workspace with multi-tab support",
    placeholder: "Chat workspace will appear here.",
    title: "Chat",
  },
  downloads: {
    description: "Download and manage AI models and inference engines",
    title: "Downloads",
  },
  security: {
    description: "FileGuard security policy and permission management",
    title: "Security",
  },
  service: {
    description: "Local service supervisor — status, restart, logs.",
    title: "Service",
  },
  settings: {
    build_info_error: "Failed to load build information.",
    build_info_heading: "Build information",
    build_info_loading: "Loading build information…",
    description: "Application preferences and build information.",
    field_data_dir: "Data directory",
    field_edition: "Edition",
    field_name: "Name",
    field_python_path: "Python path",
    field_version: "Version",
    title: "Settings",
  },
  skills: {
    description: "Skill registry and review queue. Implemented alongside Chat in PR-053.",
    title: "Skills",
  },
};

export default views;
