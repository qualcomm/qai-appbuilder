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

const channels = {
  callbackUrl: "Callback URL",
  configure: "Configure",
  connect: "Connect",
  connected: "Connected",
  copied: "Callback URL copied",
  autoStart: "Auto-start this channel on service start",
  autoStartSaveFailed: "Failed to update auto-start",
  configSaved: "Feishu config saved",
  configSaveFailed: "Failed to save Feishu config",
  settingsSaved: "Channel settings saved",
  settingsSaveFailed: "Failed to save channel settings",
  settingsLoadFailed: "Failed to load channel settings",
  legacy_notice:
    "This view uses the legacy component path. Please update to FeishuConfigPanel.",
  modelLabel: "AI Model",
  modelFollowGlobal: "Default (follow global settings)",
  modelSearch: "Search models...",
  modelNoCandidates: "No cloud models configured",
  modelSaved: "Channel model saved",
  modelSaveFailed: "Failed to save channel model",
  proxyTitle: "Proxy Settings",
  proxyLabel: "Proxy",
  proxyAddress: "Proxy Address",
  proxyAddressPlaceholder: "http://proxy:8080",
  proxyUsername: "Username",
  proxyUsernamePlaceholder: "(optional)",
  proxyPassword: "Password",
  proxyPasswordPlaceholder: "(unchanged)",
  proxySyncGlobal: "Sync Global Proxy",
  proxySynced: "Synced from global proxy (remember to save)",
  proxySaved: "Proxy settings saved",
  proxySaveFailed: "Failed to save proxy settings",
  settings_btn: "Settings",
  start: "Start",
  starting: "Starting Feishu channel...",
  started: "Feishu channel started.",
  stop: "Stop",
  stopping: "Stopping Feishu channel...",
  stopped: "Feishu channel stopped.",
  disableChannel: "Disable Channel",
  disconnect: "Disconnect",
  disconnected: "Disconnected",
  enableChannel: "Enable Channel",
  scan_wechat: "Scan the QR code above with WeChat",
  qr_scanned: "Scanned! Confirm on your phone.",
  qr_expired: "QR code expired",
  qr_countdown: "Refreshes in {seconds}s",
  qr_refresh: "Refresh QR",
  qr_reget: "Get new QR code",
  feishu: {
    cardDesc: "Connect via Feishu Open Platform WebSocket long-connection, no public IP required",
    connect: "Connect Feishu",
    connectedMsg: "Feishu connected, receiving messages",
    connectingMsg: "Connecting to Feishu server...",
    errorMsg: "Channel error",
    idleHint: "Configure Feishu app credentials to connect via WebSocket, no public IP required",
    introLine1: "Receives messages via Feishu Open Platform WebSocket long-connection - no public IP required.<br>",
    // introLine2 split: pull the "Feishu Open Platform" link out of the
    // inline sentence into a stand-alone button-style external link. The
    // paragraph now holds only prose (no <a>); the button is built in
    // FeishuConfigPanel's template.
    introLine2Prefix: "Create a custom app on the Feishu Open Platform, enable the bot capability, request <code>im:message</code> and <code>im:message:send_as_bot</code> permissions, then in \"Event Subscription\" choose WebSocket long-connection mode and subscribe to the <code>im.message.receive_v1</code> event.",
    openPlatformLabel: "Open Feishu Open Platform",
    openPlatformTooltip: "Go to Feishu Open Platform",
    refreshStatus: "Refresh Status",
    runningHint: "Feishu channel is running. The bot is connected to Feishu and ready to receive messages.",
    info: {
      btnTitle: "Feishu channel guide",
      subtitle: "Feishu Open Platform · WebSocket Long-connection",
      title: "Feishu Channel Guide",
      stepsTitle: "⚙️ Setup Steps",
      step1: "Create a custom app on the <a href=\"https://open.feishu.cn/app\" target=\"_blank\" style=\"color:var(--accent)\">Feishu Open Platform</a>",
      step2: "Enable the <strong>Bot</strong> capability",
      step3: "Request the <code>im:message</code> and <code>im:message:send_as_bot</code> permissions",
      step4: "Choose <strong>WebSocket long-connection</strong> mode under \"Event Subscription\"",
      step5: "Subscribe to the <code>im.message.receive_v1</code> event",
      step6: "Fill in the App ID and App Secret and save",
      advantagesTitle: "✅ Advantages",
      adv1: "<strong>No public IP needed</strong>: receives messages via WebSocket long-connection, no server required",
      adv2: "<strong>No QR scan</strong>: authenticates directly with app credentials (App ID + Secret), auto-reconnects on restart",
      adv3: "<strong>Image support</strong>: AI automatically recognizes and describes images in Feishu messages",
      adv4: "<strong>Auto model switch</strong>: switches to a cloud model when the local model is unavailable, with advance notice",
      notesTitle: "⚠️ Notes",
      note1: "Each Feishu user's conversation history is <strong>stored independently</strong> and viewable in the Chat view",
      note2: "If your corporate network uses a proxy, configure it under \"Proxy Settings\"",
      note3: "App Secret is a sensitive field; shown as **** after saving and never stored in plaintext",
    },
    name: "Feishu",
    status: {
      error: "Error",
      running: "Connected",
      starting: "Connecting...",
      stopped: "Not connected",
    },
  },
  feishuDesc: "Connect Feishu (Lark) robot messaging",
  guideBtn: "Usage Guide",
  settingsBtn: "Channel Settings",
  guide: {
    subtitle: "WeChat · Feishu · Common Commands",
    title: "Bot Channel Usage Guide",
    smartTitle: "🔄 Smart Model Switching",
    smart1: "When the channel's configured model is a <strong>local model</strong> (or follows the global setting which points to a local model), if the local model service is <strong>not running or unavailable</strong>, the system automatically detects this and attempts a fallback.",
    smart2: "If an <strong>available cloud model</strong> is detected, the system sends a notice before replying:<br><em style=\"color:var(--text-secondary)\">⚠️ The local model is currently unavailable; automatically switched to cloud model: xxx</em>",
    smart3: "If <strong>no cloud model is configured</strong>, it still tries to route to the local service, and the message may fail. Configuring at least one cloud model as a fallback is recommended.",
    smart4: "Once the local model recovers, send <strong>/model 0</strong> to follow the global setting again.",
  },
  cmd: {
    sectionTitle: "⌨️ Conversation Commands",
    help: "Show the full command help",
    new: "Save the current session, then start a new one",
    clear: "Delete the current session history (without saving), then start a new one",
    list: "View the most recent N sessions (default 5)",
    use: "Switch to a specific session by number",
    status: "View the current session status (name, round count)",
    rename: "Rename the current session",
    delete: "Delete the current session (irreversible) and start a new one",
    stop: "Immediately stop the currently running task",
    models: "View the list of available models",
    model: "View/switch the model; /model 0 restores the global setting",
    compact: "View/temporarily change the session's history rounds",
    reboot: "Restart the whole service; WeChat and Feishu channels auto-reconnect afterward",
    rebootWechat: "Restart the service; the WeChat channel auto-reconnects afterward",
    rebootFeishu: "Restart the service; the Feishu channel auto-reconnects afterward",
    helpFull:
      "Show this usage guide (all available commands).<br><em>Reply: Complete command help</em>",
    newFull:
      "<strong>Save</strong> current session history and start new; history remains in Chat interface.<br><em>Reply: New session started ✨</em>",
    clearFull:
      "<strong>Delete</strong> current session history (unsaved) and start new; record permanently removed from Chat.<br><em>Reply: Session cleared 🗑</em>",
    listFull: "View last N saved sessions (default 5), showing name, time and rounds.",
    useFull: "Switch to the specified saved session by number.",
    statusFull: "View current session status (name, rounds, context size).",
    renameFull: "Rename current session.",
    deleteFull: "Delete current session (irreversible) and start new.",
    stopFull:
      "<strong>Immediately stop</strong> the current running task (both chat and Claude Code tasks supported).<br><em>Reply: ⏹️ Task stopped, you can send a new message.</em>",
    modelsFull:
      "View all <strong>available models</strong> (local + cloud) and show currently used model.<br><em>Reply: Numbered model list</em>",
    modelFull:
      "<strong>Switch model</strong> by number from <strong>/models</strong> list, or enter model_id directly. Send <strong>/model 0</strong> to restore global setting.<br><em>Reply: ✅ Switched to model: xxx</em>",
    compactFull:
      "View or <strong>temporarily change</strong> current session retained history rounds.<br><em>Reply: ✅ Session history rounds set to n</em>",
    rebootFull:
      "Restart the entire QAIModelBuilder service; WeChat and Feishu channels auto-reconnect after restart.<br><em>Reply: System restarting, please wait... 🔄</em>",
  },
  help: "Help",
  settings: {
    history: {
      desc: "Shared by Feishu + WeChat channels. Oldest complete rounds are removed when limit is exceeded.",
      hint: "Maximum history rounds retained by Feishu/WeChat channels. One round = one user message + all tool calls + final AI reply.",
      label: "Conversation History Rounds",
      unit: "rounds",
    },
    subtitle: "Shared settings for Feishu · WeChat channels",
    title: "Channel Settings",
  },
  status: "Status",
  subtitle: "Manage messaging channels and connect external platforms",
  title: "Channels",
  wechat: {
    cardDesc: "Connect personal WeChat via iLink Bot, send/receive text and image messages",
    info: {
      btnTitle: "WeChat channel guide",
      subtitle: "iLink Bot · Personal WeChat",
      title: "WeChat Channel Guide",
      notesTitle: "⚠️ Notes",
      note1: "This channel connects personal WeChat via <strong>iLink Bot</strong>; scan the QR on your phone to authorize.",
      note2: "Credentials are stored locally, so the channel usually <strong>auto-reconnects</strong> after a service restart with no re-scan needed.",
      note3: "If unused for a long time or the WeChat account logs in on another device, credentials may expire and a re-scan is required.",
      note4: "Each WeChat user's conversation history is <strong>stored independently</strong> and viewable in the Chat view.",
      note5: "Supports sending <strong>image messages</strong>; the AI automatically recognizes and describes image content.",
    },
    name: "WeChat (Personal)",
    connect: "Connect WeChat",
    rescan: "Re-scan login",
    rescanTitle: "Force a fresh QR scan (ignore saved credentials)",
    connectedMsg: "WeChat connected, receiving messages",
    refreshStatus: "Refresh Status",
    runningHint: "WeChat channel running; the bot is connected and can receive messages.",
    errorMsg: "Channel error",
    idleHint: "Scan with WeChat to connect and start messaging",
    status: {
      connected: "Connected",
      error: "Error",
      expired: "Expired",
      idle: "Not connected",
      logging_in: "Waiting for scan",
      scanned: "Scanned",
    },
  },
  wechatDesc: "Connect WeChat for Work robot messaging",
};

export default channels;
