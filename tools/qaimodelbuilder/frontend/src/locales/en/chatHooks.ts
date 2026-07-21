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

const chatHooks = {
  title: "Hook Management",
  subtitle:
    "Shell commands the AI runs automatically at the given event point",
  empty: "No hooks configured yet. Click \"Add Hook\" to create one.",
  loadFailed: "Failed to load hooks",
  saveFailed: "Failed to save hooks",
  saved: "Hooks saved",
  field: {
    event: "Event",
    command: "Command",
    timeout: "Timeout (seconds)",
  },
  placeholder: {
    command: "e.g. ruff check .",
  },
  action: {
    add: "Add Hook",
    delete: "Delete",
    save: "Save",
    saving: "Saving…",
  },
  confirm: {
    deleteTitle: "Delete hook",
    deleteMessage: "Delete this hook? This cannot be undone.",
    deleteConfirm: "Delete",
    cancel: "Cancel",
  },
  enable: {
    label: "Enable hooks",
    securityWarning:
      "Enabling lets your configured shell commands run automatically (arbitrary command execution). Only enable this if you trust every hook command.",
    disabledHint:
      "Hooks are disabled. You can still edit the configuration below, but no commands will run until you enable hooks.",
    loadFailed: "Failed to load hooks enabled state",
    saveFailed: "Failed to update hooks enabled state",
    savedOn: "Hooks enabled",
    savedOff: "Hooks disabled",
  },
  docs: {
    title: "Steering a tool call from a pre_tool_call hook",
    intro:
      "A pre_tool_call hook can steer the tool call by printing JSON on stdout (exit 0). Recognised keys:",
    deny: "block the call; the model receives \"[hook_blocked] {reason}\".",
    allow: "proceed (this is the default).",
    updatedInput: "replace the tool's arguments before it runs.",
    additionalContext:
      "(for pre_message / on_user_input hooks) fold extra text into the turn.",
    observer:
      "Plain / non-JSON output makes the hook an observer only (unchanged behaviour).",
    exampleLabel: "Example:",
  },
  subagents: {
    title: "Sub-agent models",
    subtitle:
      "Choose which model each sub-agent profile uses. Leave as inherit to use the main chat model.",
    inherit: "(inherit main model)",
    loadFailed: "Failed to load sub-agent models",
    saveFailed: "Failed to save sub-agent model",
    saved: "Sub-agent model saved",
    profile: {
      explore: {
        label: "Explore",
        desc: "Read-only search specialist for codebase exploration.",
      },
      general: {
        label: "General",
        desc: "Full-tool sub-agent for general-purpose tasks.",
      },
    },
  },
  event: {
    pre_tool_call: "Before tool call",
    post_tool_call: "After tool call",
    pre_message: "Before message",
    post_message: "After message",
    on_error: "On error",
    on_complete: "On complete",
    on_user_input: "On user input",
    on_session_start: "On session start",
    on_session_end: "On session end",
    on_truncate: "On truncate",
  },
};

export default chatHooks;
