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
  agentLoopTitle: "Agent 循环",
  aiCodingDesc: "控制聊天输入框上方 Claude Code / Open Code 模式按钮的显隐。关闭后对应按钮从工具栏隐藏，后端会话保留不受影响。",
  aiCodingTitle: "AI 编程助手",
  allowExecDesc: "启用后，AI 助手可通过 <code>exec</code> 工具执行系统命令（运行脚本、调用 CLI 等）。<br>当 <b>绑定地址为 0.0.0.0</b> 时，关闭此选项可防止局域网设备通过 API 触发任意命令执行。暴露到局域网时建议关闭。",
  allowExecLabel: "允许执行工具",
  sslVerifyLabel: "校验 TLS/SSL 证书",
  sslVerifyDesc: "开启后，所有出站 HTTPS 连接（模型服务、webfetch、MCP、登录）都会校验服务器的 TLS 证书。若你的模型服务使用自签名证书或企业网关证书，请<b>关闭</b>此项。修改对 webfetch 工具立即生效；对模型服务连接需重启后完全生效。",
  sslVerifyRebootTitle: "是否重启以生效？",
  sslVerifyRebootMessage: "TLS 校验的修改需重启后才能对模型服务及其他连接完全生效。现在重启吗？",
  sslVerifyRebootConfirm: "立即重启",
  sslVerifyRebootCancel: "稍后",
  sslVerifyRebootDeferred: "已保存。稍后重启以完全生效。",
  sslVerifySaved: "TLS 校验设置已保存。",
  autoCompressDesc: "接近 token 上限时自动压缩上下文",
  autoCompressLabel: "自动压缩",
  autoSave: "修改时自动保存",
  autoTitleDesc: "自动生成对话标题",
  autoTitleLabel: "自动标题",
  bindAddressChangedToast: "⚠️ 绑定地址已修改，请重启 QAIModelBuilder 使其生效。",
  bindAddressDesc: "控制 QAIModelBuilder 监听的网络接口。<br><b>127.0.0.1</b>（推荐）：仅本机可访问，最安全。<br><b>0.0.0.0</b>（调试/局域网模式）：局域网内所有设备均可访问，会将所有 API 端点（含文件读写和 exec 工具）暴露给局域网设备，仅在可信网络中使用。<b>修改后需重启生效。</b>",
  bindAddressLabel: "WebUI 绑定地址",
  bindAll: "0.0.0.0 — 所有接口，局域网可访问（调试模式）",
  bindHostDesc: "控制 QAIModelBuilder 监听的网络接口。<br><b>127.0.0.1</b>（推荐）：仅本机可访问，最安全。<br><b>0.0.0.0</b>（调试/局域网模式）：局域网内所有设备均可访问，会将所有 API 端点（含文件读写和 exec 工具）暴露给局域网设备，仅在可信网络中使用。<b>修改后需重启生效。</b>",
  bindHostLabel: "WebUI 绑定地址",
  bindLocalOnly: "127.0.0.1 — 仅本机访问（推荐）",
  channelsTitle: "通道",
  chatDisplayTitle: "对话显示",
  chatDisplayDesc: "控制对话区域中消息的渲染方式。这些偏好会在重启后保留。",
  showToolCallsLabel: "显示工具调用卡片",
  showToolCallsDesc: "启用后，工具调用卡片（参数 + 输出）会内嵌显示在助手回复中。<b>关闭</b>后仅隐藏对话视图中的卡片显示——工具调用照常执行、历史记录照常保留。大多数用户保持开启；希望对话视图更简洁时可关闭。",
  compactionProtectDesc: "最近的对话中有多少（占模型窗口的百分比）始终保留原文、永不压缩。默认 35%。",
  compactionProtectLabel: "最近对话保护大小",
  compactionTargetDesc: "压缩后，把上下文缩减到模型窗口的百分之几。越低压缩越激进（例如 35% 会把 200K 窗口压到约 70K）。默认 35%。",
  compactionTargetLabel: "压缩后保留大小",
  debugTitle: "调试",
  appBuilderTitle: "应用构建器",
  appBuilderDesc: "应用构建器设置。默认进入应用构建器模式时不会打开较重的模型工作台——你在工具栏中选择已导入的模型，由 Agent 帮你构建 WebUI 应用。",
  showWorkbench: "显示模型工作台",
  showWorkbenchDesc: "开启后，进入应用构建器模式会打开完整的模型试用/运行工作台（运行、性能、历史、对比）。默认关闭；无论开关与否，工作台及其全部功能都保留。",
  modeIntroTitle: "模式引导提示",
  modeIntroDesc: "App Builder / GoMaster / Model Builder 模式内的引导卡片可被「不再显示」勾选后永久关闭。在此处重新打开开关即可恢复显示。",
  modeIntroAppBuilder: "显示 App Builder 引导",
  modeIntroGomaster: "显示 GoMaster 引导",
  modeIntroModelBuilder: "显示 Model Builder 引导",
  modeIntroModelHub: "显示 Model Hub 引导",
  modeIntroPro: "显示增强模式引导",
  modeIntroCode: "显示编程模式引导",
  enableCCInToolbar: "在工具栏中启用 Claude Code",
  enableCCInToolbarDesc: "启用后，聊天输入框上方显示 Claude Code 按钮（🤖）。点击进入 Claude Code 模式；右键退出。",
  enableOCInToolbar: "在工具栏中启用 Open Code",
  enableOCInToolbarDesc: "启用后，聊天输入框上方显示 Open Code 按钮（🔷）。点击进入 Open Code 模式；右键退出。",
  experienceExtractionDesc: "任务成功后自动提取可复用经验",
  experienceExtractionLabel: "经验沉淀",
  forgeConfigPath: "配置文件路径",
  lanWarning: "⚠️ <b>已启用局域网访问。</b>局域网内任何设备均可访问 WebUI 及所有 API 端点，包括文件读写和 exec 工具。请仅在可信网络中使用。建议同时关闭下方的 <b>Allow Exec Tool</b> 以降低风险。",
  logBufferDesc: "服务日志缓冲区大小（行数）。后端最多保留此行数的日志，前端 UI 也以此为上限显示。<br>修改后下次启动 GenieAPIService 时生效。建议范围：1000～20000。",
  logBufferHint: "默认值：6000 行。",
  logBufferLabel: "服务日志缓冲区大小",
  maxHistoryRoundsDesc: "WebUI Chat、微信通道、飞书通道共用的对话历史保留轮次上限。<br>一轮 = 一条用户消息 + 期间所有工具调用 + 最终 AI 回复。<br>超过此轮次的旧消息将在每次对话后自动从内存中移除（不影响已持久化的历史记录）。<br>WebUI Chat 可通过 <code>/compact &lt;轮次&gt;</code> 命令手动裁剪当前会话历史。",
  maxHistoryRoundsHint: "默认值：20 轮。建议范围：5～50 轮。值越大，上下文越丰富，但 prompt 越长。",
  maxHistoryRoundsLabel: "最大历史轮次",
  maxIterationsDesc: "Agentic loop 最大工具调用轮次",
  maxIterationsLabel: "最大迭代次数",
  orderLabel: "顺序：",
  proxyDesc: "适用于版本检查、模型目录下载、云端 AI 模型 API 等所有网络请求。<br>代理密码通过系统安全存储保存，不写入配置文件。",
  proxyPassword: "密码",
  proxyPasswordPlaceholder: "留空则无认证（显示 **** 表示已设置密码）",
  proxySaveBtn: "保存代理设置",
  proxySaving: "保存中…",
  proxyTitle: "网络代理",
  proxyUrl: "代理地址",
  proxyUrlPlaceholder: "http://proxy.company.com:8080（留空则不使用代理）",
  proxyUsername: "用户名",
  proxyUsernamePlaceholder: "留空则无认证",
  resetBtn: "重置",
  saveBtn: "保存设置",
  savingBtn: "保存中…",
  securityTitle: "安全",
  showPromptDesc: "开启后，每次 AI 回复消息旁显示剪贴板按钮，点击可查看本次请求发送给模型的完整提示词（含系统提示词和历史消息）。快照仅保存在内存中，重启服务后清空。",
  showPromptLabel: "在 UI 中显示提示词",
  title: "应用设置",
  toolbarModulesDesc: "控制聊天输入框底部快捷模块按钮的显隐。关闭某模块后，对应按钮将从工具栏中隐藏（已设置的会话不受影响）。",
  toolbarModulesTitle: "工具栏模块",
  workspaceModelRootDesc: "模型转换产物的存放根目录，默认 C:\\WoS_AI。",
  workspaceModelRootLabel: "模型工作区目录",
  workspaceModelRootPlaceholder: "C:\\WoS_AI",
  workspaceTitle: "工作区",
};

export default appConfig;
