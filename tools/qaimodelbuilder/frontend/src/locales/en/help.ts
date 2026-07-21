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

const help = {
  about: "About",
  ccPanelHtml: "(See claude help in Settings)",
  ccText: "Claude Code commands\n\n  /cc new <directory> [session name]\n    Create a new Claude Code session bound to a project directory.\n    Example: /cc new C:\\Projects\\MyApp Auth feature\n\n  /cc list  (/cc l)\n    List all your Claude Code sessions (ID, name, status).\n\n  /cc use <number>  (/cc u <number>)\n    Switch session by /cc list number (e.g. /cc use 1).\n\n  /cc use <ID prefix>\n    Switch session by ID prefix (first 8 chars).\n\n  /cc status  (/cc s)\n    View current session status, turn count and tool calls.\n\n  /cc models  (/cc ms)\n    View Claude Code available models with numbers.\n\n  /cc model  (/cc m)\n    View current Claude Code model.\n\n  /cc model <number>  (/cc m <number>)\n    Switch Claude Code model by number.\n\n  /cc fork  (/cc f)\n    Fork current session to a new branch (preserves history).\n\n  /cc stop  (/cc st)\n    Stop current Claude Code task.\n\n  /cc cd [directory]\n    View or change the working directory of current CC session.\n\n  /cc rename <new name>  (/cc r <new name>)\n    Rename current Claude Code session.\n\n  /cc close  (/cc c)\n    Exit CC mode (session preserved).\n\n  /cc delete  (/cc d)\n    Permanently delete current Claude Code session.\n\n  /cc help  (/cc h)\n    Show this help.\n\nTip: After creating a session, just send messages to chat with Claude Code\nTip: /cc fork is useful for trying alternative directions\nTip: /cc stop allows immediate new messages\nTip: /cc close keeps session, /cc use to re-enter\nTip: Send /new to switch back to normal AI chat mode",
  commands: "Commands",
  mainText: "Help - WeChat / Feishu / Chat commands\n\nGeneral commands:\n\n  /help  (/h)\n    Show this help.\n\n  /new  (/n)\n    Save current session history and start a new session.\n\n  /clear  (/cl)\n    Delete current history (no save) and start a new session.\n\n  /list [N]  (/l [N])\n    View recent N sessions (default 5).\n\n  /use <number>  (/u <number>)\n    Switch to specified session.\n\n  /status  (/s)\n    View current session status.\n\n  /rename <name>  (/rn <name>)\n    Rename current session.\n\n  /delete  (/del)\n    Delete current session.\n\n  /stop  (/st)\n    Stop current task.\n\n  /models  (/ms)\n    View available models.\n\n  /model  (/m)\n    View current session model.\n\n  /model <number>  (/m <number>)\n    Switch model. /model 0 to follow global settings.\n\n  /compact [rounds]  (/c [rounds])\n    Trim history to last N rounds.\n\n  /reboot  (/r)\n    Restart QAIModelBuilder service.\n\nClaude Code commands:\n\n  /cc new <dir> [name]\n  /cc list  (/cc l)\n  /cc use <num>  (/cc u <num>)\n  /cc status  (/cc s)\n  /cc models  (/cc ms)\n  /cc model  (/cc m)\n  /cc fork  (/cc f)\n  /cc stop  (/cc st)\n  /cc cd [dir]\n  /cc rename <name>  (/cc r <name>)\n  /cc close  (/cc c)\n  /cc delete  (/cc d)\n  /cc help  (/cc h)\n\nOpen Code commands:\n\n  /oc new <dir> [name]\n  /oc list  (/oc l)\n  /oc use <num>  (/oc u <num>)\n  /oc status  (/oc s)\n  /oc models  (/oc ms)\n  /oc model  (/oc m)\n  /oc stop  (/oc st)\n  /oc rename <name>  (/oc r <name>)\n  /oc close  (/oc c)\n  /oc delete  (/oc d)\n  /oc help  (/oc h)\n\nTip: Local model unavailable? Cloud model auto-switch is supported.\nClaude Code: enable in Settings > AI Coding.\nOpen Code: enable in Settings > AI Coding > Open Code.",
  ocPanelHtml: "(See OpenCode help in Settings)",
  ocText: "Open Code commands\n\n  /oc new <directory> [session name]\n    Create a new Open Code session bound to a project directory.\n    Example: /oc new C:\\Projects\\MyApp Auth feature\n\n  /oc list  (/oc l)\n    List all your Open Code sessions (ID, name, status, model).\n\n  /oc use <number>  (/oc u <number>)\n    Switch session by /oc list number (e.g. /oc use 1).\n\n  /oc use <ID prefix>\n    Switch session by ID prefix (first 8 chars).\n\n  /oc status  (/oc s)\n    View current session status, turn count and tool calls.\n\n  /oc models  (/oc ms)\n    View Open Code available models with numbers.\n\n  /oc model  (/oc m)\n    View current Open Code model.\n\n  /oc model <number>  (/oc m <number>)\n    Switch Open Code model by number.\n\n  /oc stop  (/oc st)\n    Stop current Open Code task.\n\n  /oc rename <new name>  (/oc r <new name>)\n    Rename current Open Code session.\n\n  /oc close  (/oc c)\n    Exit OC mode (session preserved).\n\n  /oc delete  (/oc d)\n    Permanently delete current Open Code session.\n\n  /oc help  (/oc h)\n    Show this help.\n\nTip: After creating a session, just send messages to chat with Open Code\nTip: /oc stop allows immediate new messages\nTip: /oc close keeps session, /oc use to re-enter\nTip: Send /new to switch back to normal AI chat mode\nTip: Open Code: enable in Settings > AI Coding > Open Code",
  shortcuts: "Keyboard Shortcuts",
  title: "Help",
  version: "Version",
  webuiCcHint:
    "💡 No /cc command needed in the web UI: click the “Claude Code” button above the input box to enter CC mode, then create / switch / rename / delete sessions in the session panel.\n(The /cc commands still work in the WeChat / Feishu channels.)",
  webuiOcHint:
    "💡 No /oc command needed in the web UI: click the “Open Code” button above the input box to enter OC mode, then create / switch / rename / delete sessions in the session panel.\n(The /oc commands still work in the WeChat / Feishu channels.)",
};

export default help;
