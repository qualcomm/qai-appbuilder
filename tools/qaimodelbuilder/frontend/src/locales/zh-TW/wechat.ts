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
  agentId: "應用 ID",
  autoConnect: "服務啟動時自動連接微信通道",
  btn: {
    connect: "連接微信",
    disconnect: "中斷連線",
    refreshQr: "↻ 重新整理 QR Code",
    regetQr: "重新取得 QR Code",
    retry: "重試連線",
    saveTitle: "儲存設定",
  },
  configSaved: "微信設定已儲存",
  connectionFailed: "連線失敗",
  connectionOk: "連線成功",
  corpId: "企業 ID",
  enable: "啟用微信通道",
  encodingAesKey: "訊息加密金鑰",
  globalProxyNotConfigured: "全域代理未設定，無法同步",
  idle: {
    hint: "用手機微信掃碼即可連接，開始收發訊息",
  },
  label: {
    aiModel: "AI 模型",
  },
  loginFailed: "微信登入失敗",
  model: {
    default: "預設（跟隨全域設定）",
  },
  modelDefault: "預設（跟隨全域設定）",
  modelSaved: "微信模型已儲存",
  modelSavedToast: "微信模型已儲存",
  placeholder: {
    searchModel: "搜尋模型...",
  },
  proxy: {
    address: "代理位址",
    label: "代理",
    noAuthHint: "留空則無認證",
    password: "密碼",
    sectionTitle: "代理設定（企業網路可選）",
    syncGlobal: "同步全域代理",
    syncTitle: "從 設定 > 應用設定 > 網路代理 同步代理位址和使用者名稱",
    title: "代理設定",
    username: "使用者名稱",
  },
  proxyGlobalNotConfigured: "全域代理未配置，無法同步",
  proxySaved: "代理設定已儲存",
  proxySavedToast: "代理設定已儲存",
  proxySyncedFromGlobal: "已從全域代理同步位址和使用者名稱，請輸入密碼後儲存",
  proxySyncedFromGlobalToast: "已從全域代理同步位址和使用者名稱，請輸入密碼後儲存",
  qr: {
    alt: "微信登入 QR Code",
    countdown: "{seconds}s 後自動重新整理",
  },
  qrIssueFailed: "發起 QR Code 登入失敗",
  registerFailed: "註冊微信失敗",
  saveConfig: "儲存設定",
  saveConfigFailed: "儲存微信設定失敗",
  saveFailed: "儲存失敗",
  saveFailedToast: "儲存失敗：{msg}",
  secret: "金鑰",
  status: {
    connected: "微信已連線，正在接收訊息",
    error: "通道出現錯誤",
    expired: "QR Code 已過期",
    scanHint: "用手機微信掃描上方 QR Code",
    scanned: "✅ 已掃碼，請在手機上確認",
  },
  syncDisabled: "已關閉微信同步",
  syncDisabledToast: "已關閉微信同步",
  syncEnabled: "已開啟微信同步",
  syncEnabledToast: "已開啟微信同步",
  syncFailed: "同步失敗",
  syncFailedToast: "同步失敗：{msg}",
  syncSetFailed: "設定微信同步失敗",
  syncSetFailedToast: "設定微信同步失敗: {msg}",
  testConnection: "測試連線",
  title: "微信設定",
  token: "Token",
};

export default wechat;
