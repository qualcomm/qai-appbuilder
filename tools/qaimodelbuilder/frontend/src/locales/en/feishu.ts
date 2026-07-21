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

const feishu = {
  acknowledgeFailed: "Failed to acknowledge Feishu error",
  appId: "App ID",
  appSecret: "App Secret",
  appSecretDesc: "Sensitive field; saved value is masked as ****",
  autoConnect: "Auto-connect Feishu channel on service start",
  bindFailed: "Bind failed",
  bindFailedToast: "Bind failed: {msg}",
  btn: {
    cancel: "Cancel",
    connect: "Connect Feishu",
    disconnect: "Disconnect",
    retry: "Retry Connection",
    saveTitle: "Save configuration",
  },
  channelControl: "Channel Control",
  channelTitle: "Feishu Channel",
  configSaved: "Feishu configuration saved",
  configSavedToast: "Feishu configuration saved",
  connectionFailed: "Connection failed",
  connectionOk: "Connection successful",
  enable: "Enable Feishu Channel",
  encryptKey: "Encrypt Key",
  encryptKeyDesc: "If event encryption is enabled in the Feishu app, fill the encrypt key; otherwise leave empty",
  globalProxyNotConfigured: "Global proxy not configured, cannot sync",
  idle: {
    hint: "Configure Feishu app credentials to connect via WebSocket, no public IP required",
  },
  info: "Feishu Info",
  intro: {
    line1: "Receives messages via Feishu Open Platform WebSocket long-connection — no public IP required.<br>",
    line2: "Visit <a href=\"https://open.feishu.cn/app\" target=\"_blank\" style=\"color:var(--accent)\">Feishu Open Platform</a> to create a custom app, enable the bot capability, request <code>im:message</code> and <code>im:message:send_as_bot</code> permissions, then in \"Event Subscription\" choose WebSocket long-connection mode and subscribe to the <code>im.message.receive_v1</code> event.",
  },
  label: {
    aiModel: "AI Model",
    appId: "App ID",
    appSecret: "App Secret",
    appSecretSaved: "Saved",
    encryptKey: "Encrypt Key",
    verifyToken: "Verify Token",
  },
  model: {
    default: "Default (follow global settings)",
  },
  modelDefault: "Default (follow global settings)",
  modelSaved: "Feishu model saved",
  notifyBound: "Feishu notification bound to {id}",
  notifyBoundToast: "Feishu notification bound to {id}",
  notifyCleared: "Feishu notification cleared",
  notifyClearedToast: "Feishu notification cleared",
  optionalSuffix: "(optional)",
  placeholder: {
    appSecret: "Enter App Secret",
    encryptKey: "Fill when event encryption is enabled (optional)",
    encryptKeyShort: "Fill when event encryption is enabled",
    saved: "Saved (enter new value to update)",
    searchModel: "Search models...",
    verifyToken: "Event verification Token (optional)",
  },
  proxy: {
    address: "Proxy Address",
    label: "Proxy",
    noAuthHint: "Leave empty for no authentication",
    password: "Password",
    sectionTitle: "Proxy Settings (optional for corporate networks)",
    syncGlobal: "Sync Global Proxy",
    syncTitle: "Sync from global proxy",
    title: "Proxy settings",
    username: "Username",
  },
  proxySaved: "Proxy settings saved",
  proxySyncedFromGlobal: "Synced address and username from global proxy, please enter password and save",
  refreshStatus: "Refresh Status",
  registerFailed: "Failed to register Feishu",
  runningHint: "Feishu channel is running. The bot is connected to Feishu servers and can receive messages.",
  saveConfig: "Save Configuration",
  saveConfigFailed: "Failed to save Feishu config",
  saveFailed: "Save failed",
  saveFailedToast: "Save failed: {msg}",
  savingShort: "Saving...",
  start: "Start",
  startFailed: "Feishu channel start failed",
  startFailedFallback: "Start failed",
  startFailedToast: "Feishu channel start failed: {msg}",
  starting: "Feishu channel starting...",
  startingToast: "Feishu channel starting...",
  status: {
    connected: "Feishu connected, receiving messages",
    connecting: "Connecting to Feishu server...",
    error: "Channel error occurred",
  },
  statusText: {
    errorShort: "Error",
    running: "Running",
    starting: "Starting...",
    stopped: "Stopped",
  },
  stop: "Stop",
  stopChannelFailed: "Failed to stop Feishu",
  stopFailed: "Stop failed",
  stopFailedToast: "Stop failed: {msg}",
  stopped: "Feishu channel stopped",
  stoppedToast: "Feishu channel stopped",
  syncDisabled: "Feishu sync disabled",
  syncEnabled: "Feishu sync enabled",
  syncFailed: "Sync failed",
  syncSetFailed: "Failed to set Feishu sync",
  testConnection: "Test Connection",
  title: "Feishu Configuration",
  verificationToken: "Verification Token",
};

export default feishu;
