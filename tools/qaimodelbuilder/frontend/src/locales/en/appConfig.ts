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

const appConfig = {
  agentLoopTitle: "Agent Loop",
  aiCodingDesc: "Show or hide the Claude Code / Open Code mode pills in the chat input toolbar. Disabling a pill hides its button; existing sessions remain on the backend.",
  aiCodingTitle: "AI Coding Assistants",
  allowExecDesc: "When enabled, the AI assistant can execute system commands via the <code>exec</code> tool (run scripts, invoke CLI, etc.).<br>When <b>Bind Address is 0.0.0.0</b>, disabling this option prevents LAN devices from triggering arbitrary command execution via the API. Recommended to disable when exposed to LAN.",
  allowExecLabel: "Allow Exec Tool",
  sslVerifyLabel: "Verify TLS/SSL certificates",
  sslVerifyDesc: "When on, all outbound HTTPS connections (model service, webfetch, MCP, sign-in) verify the server's TLS certificate. Turn <b>off</b> if your model service uses a self-signed or corporate-gateway certificate. Applies immediately to the webfetch tool; a restart is required for it to take full effect on the model-service connection.",
  sslVerifyRebootTitle: "Restart to apply?",
  sslVerifyRebootMessage: "The TLS verification change takes full effect on the model-service and other connections only after a restart. Restart now?",
  sslVerifyRebootConfirm: "Restart now",
  sslVerifyRebootCancel: "Later",
  sslVerifyRebootDeferred: "Saved. Restart later for it to fully take effect.",
  sslVerifySaved: "TLS verification setting saved.",
  autoCompressDesc: "Automatically compress context when approaching token limit",
  autoCompressLabel: "Auto Compress",
  autoSave: "Auto-save on change",
  autoTitleDesc: "Automatically generate conversation titles",
  autoTitleLabel: "Auto Title",
  bindAddressChangedToast: "⚠️ Bind Address has been changed. Please restart QAIModelBuilder for it to take effect.",
  bindAddressDesc: "Controls which network interface QAIModelBuilder listens on.<br><b>127.0.0.1</b> (recommended): Only accessible locally, most secure.<br><b>0.0.0.0</b> (debug/LAN mode): Accessible by all devices on the LAN; exposes all API endpoints (including file read/write and exec tool) to LAN devices. Use only on trusted networks. <b>Requires restart to take effect.</b>",
  bindAddressLabel: "WebUI Bind Address",
  bindAll: "0.0.0.0 — All interfaces, LAN accessible (debug mode)",
  bindHostDesc: "Controls which network interface QAIModelBuilder listens on.<br><b>127.0.0.1</b> (recommended): Only accessible locally, most secure.<br><b>0.0.0.0</b> (debug/LAN mode): Accessible by all devices on the LAN; exposes all API endpoints (including file read/write and exec tool) to LAN devices. Use only on trusted networks. <b>Requires restart to take effect.</b>",
  bindHostLabel: "WebUI Bind Address",
  bindLocalOnly: "127.0.0.1 — Localhost only (recommended)",
  channelsTitle: "Channels",
  chatDisplayTitle: "Chat Display",
  chatDisplayDesc: "Control how the chat area renders individual messages. These preferences persist across restarts.",
  showToolCallsLabel: "Show tool-call cards",
  showToolCallsDesc: "When enabled, tool-call cards (arguments + output) appear inline in the assistant's replies. Turn <b>off</b> to hide them from the chat view — the calls still execute and are still recorded in history; only the on-screen cards are collapsed. Most users leave this on; disable it when you want the chat to feel more like a plain conversation.",
  compactionProtectDesc: "How much of the most-recent conversation (as a percentage of the model window) is always kept verbatim and never compressed. Default 35%.",
  compactionProtectLabel: "Recent History Protection",
  compactionTargetDesc: "After compression, how much of the model window the context is shrunk down to. Lower = more aggressive compression (e.g. 35% shrinks a 200K window to ~70K). Default 35%.",
  compactionTargetLabel: "Post-compression Keep Size",
  debugTitle: "Debug",
  appBuilderTitle: "App Builder",
  appBuilderDesc: "App Builder settings. By default, entering App Builder mode does not open the heavy model workbench — you select imported models from the toolbar and let the Agent help you build a WebUI app.",
  showWorkbench: "Show model workbench",
  showWorkbenchDesc: "When enabled, entering App Builder mode opens the full model test/run workbench (run, metrics, history, compare). Off by default; the workbench and all its features are retained regardless.",
  modeIntroTitle: "Mode Intro Hints",
  modeIntroDesc: "The in-conversation intro card for App Builder / GoMaster / Model Builder can be permanently dismissed via its \"Don't show again\" checkbox. Turn these back on here to restore them.",
  modeIntroAppBuilder: "Show App Builder intro",
  modeIntroGomaster: "Show GoMaster intro",
  modeIntroModelBuilder: "Show Model Builder intro",
  modeIntroModelHub: "Show Model Hub intro",
  modeIntroPro: "Show Pro-mode intro",
  modeIntroCode: "Show Code-mode intro",
  enableCCInToolbar: "Enable Claude Code in toolbar",
  enableCCInToolbarDesc: "When enabled, the Claude Code pill (🤖) appears in the chat input toolbar. Click the pill to enter Claude Code mode; right-click to exit.",
  enableOCInToolbar: "Enable Open Code in toolbar",
  enableOCInToolbarDesc: "When enabled, the Open Code pill (🔷) appears in the chat input toolbar. Click the pill to enter Open Code mode; right-click to exit.",
  experienceExtractionDesc: "Auto-extract reusable experiences after successful tasks",
  experienceExtractionLabel: "Experience Extraction",
  forgeConfigPath: "Configuration file path",
  lanWarning: "⚠️ <b>LAN access enabled.</b> Any device on the LAN can access WebUI and all API endpoints, including file read/write and exec tool. Use only on trusted networks. It is recommended to disable <b>Allow Exec Tool</b> below to reduce risk.",
  logBufferDesc: "Buffer size (in lines) for Service Logs. The backend retains up to this many lines, and the frontend UI displays up to this limit.<br>Takes effect on next GenieAPIService startup. Recommended range: 1000-20000.",
  logBufferHint: "Default: 6000 lines.",
  logBufferLabel: "Service Log Buffer Size",
  maxHistoryRoundsDesc: "Maximum conversation history rounds shared by WebUI Chat, WeChat channel, and Feishu channel.<br>One round = one user message + all tool calls during that round + final AI reply.<br>Messages beyond this limit are automatically removed from memory after each conversation (does not affect persisted history).<br>WebUI Chat can manually trim current session history via <code>/compact &lt;rounds&gt;</code> command.",
  maxHistoryRoundsHint: "Default: 20 rounds. Recommended range: 5-50. Higher values provide richer context but longer prompts.",
  maxHistoryRoundsLabel: "Max History Rounds",
  maxIterationsDesc: "Maximum tool call rounds in agentic loop",
  maxIterationsLabel: "Max Iterations",
  orderLabel: "Order:",
  proxyDesc: "Applies to all network requests including version checks, model catalog downloads, and cloud AI model APIs.<br>Proxy password is stored securely by the system and not written to the config file.",
  proxyPassword: "Password",
  proxyPasswordPlaceholder: "Leave empty for no authentication (**** means password is set)",
  proxySaveBtn: "Save Proxy Settings",
  proxySaving: "Saving...",
  proxyTitle: "Network Proxy",
  proxyUrl: "Proxy URL",
  proxyUrlPlaceholder: "http://proxy.company.com:8080 (leave empty to disable proxy)",
  proxyUsername: "Username",
  proxyUsernamePlaceholder: "Leave empty for no authentication",
  resetBtn: "Reset",
  saveBtn: "Save Settings",
  savingBtn: "Saving...",
  securityTitle: "Security",
  showPromptDesc: "When enabled, a clipboard button appears next to each AI reply. Click to view the full prompt sent to the model for that request (including system prompt and message history). Snapshots are stored in memory only and cleared on service restart.",
  showPromptLabel: "Show Prompt in UI",
  title: "Application Settings",
  toolbarModulesDesc: "Control the visibility of quick-access module buttons at the bottom of the chat input. Disabling a module hides its button from the toolbar (existing sessions are unaffected).",
  toolbarModulesTitle: "Toolbar Modules",
  workspaceModelRootDesc: "Root directory where model conversion artifacts are stored. Default: C:\\WoS_AI.",
  workspaceModelRootLabel: "Model Workspace Directory",
  workspaceModelRootPlaceholder: "C:\\WoS_AI",
  workspaceTitle: "Workspace",
};

export default appConfig;
