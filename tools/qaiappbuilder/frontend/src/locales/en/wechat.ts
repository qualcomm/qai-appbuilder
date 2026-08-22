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

const wechat = {
  agentId: "Agent ID",
  autoConnect: "Auto-connect WeChat channel on service start",
  btn: {
    connect: "Connect WeChat",
    disconnect: "Disconnect",
    refreshQr: "↻ Refresh QR Code",
    regetQr: "Get New QR Code",
    retry: "Retry Connection",
    saveTitle: "Save configuration",
  },
  configSaved: "WeChat configuration saved",
  connectionFailed: "Connection failed",
  connectionOk: "Connection successful",
  corpId: "Corp ID",
  enable: "Enable WeChat Channel",
  encodingAesKey: "Encoding AES Key",
  globalProxyNotConfigured: "Global proxy not configured, cannot sync",
  idle: {
    hint: "Scan with WeChat to connect and start messaging",
  },
  label: {
    aiModel: "AI Model",
  },
  loginFailed: "WeChat login failed",
  model: {
    default: "Default (follow global settings)",
  },
  modelDefault: "Default (follow global settings)",
  modelSaved: "WeChat model saved",
  modelSavedToast: "WeChat model saved",
  placeholder: {
    searchModel: "Search models...",
  },
  proxy: {
    address: "Proxy Address",
    label: "Proxy",
    noAuthHint: "Leave empty for no authentication",
    password: "Password",
    sectionTitle: "Proxy Settings (optional for corporate networks)",
    syncGlobal: "Sync Global Proxy",
    syncTitle: "Sync proxy address and username from Settings > App Config > Network Proxy",
    title: "Proxy settings",
    username: "Username",
  },
  proxyGlobalNotConfigured: "Global proxy not configured, cannot sync",
  proxySaved: "Proxy settings saved",
  proxySavedToast: "Proxy settings saved",
  proxySyncedFromGlobal: "Synced address and username from global proxy, please enter password and save",
  proxySyncedFromGlobalToast: "Synced address and username from global proxy, please enter password and save",
  qr: {
    alt: "WeChat login QR code",
    countdown: "{seconds}s until auto-refresh",
  },
  qrIssueFailed: "Failed to issue QR login",
  registerFailed: "Failed to register WeChat",
  saveConfig: "Save Configuration",
  saveConfigFailed: "Failed to save WeChat config",
  saveFailed: "Save failed",
  saveFailedToast: "Save failed: {msg}",
  secret: "Secret",
  status: {
    connected: "WeChat connected, receiving messages",
    error: "Channel error occurred",
    expired: "QR code expired",
    scanHint: "Scan the QR code above with WeChat",
    scanned: "✅ Scanned, please confirm on your phone",
  },
  syncDisabled: "WeChat sync disabled",
  syncDisabledToast: "WeChat sync disabled",
  syncEnabled: "WeChat sync enabled",
  syncEnabledToast: "WeChat sync enabled",
  syncFailed: "Sync failed",
  syncFailedToast: "Sync failed: {msg}",
  syncSetFailed: "Failed to set WeChat sync",
  syncSetFailedToast: "Failed to set WeChat sync: {msg}",
  testConnection: "Test Connection",
  title: "WeChat Configuration",
  token: "Token",
};

export default wechat;
