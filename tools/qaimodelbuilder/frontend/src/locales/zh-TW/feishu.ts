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
  acknowledgeFailed: "確認飛書錯誤失敗",
  appId: "應用 ID",
  appSecret: "應用金鑰",
  appSecretDesc: "敏感欄位，儲存後顯示為 ****",
  autoConnect: "服務啟動時自動連接飛書通道",
  bindFailed: "綁定失敗",
  bindFailedToast: "綁定失敗：{msg}",
  btn: {
    cancel: "取消",
    connect: "連接飛書",
    disconnect: "中斷連線",
    retry: "重試連線",
    saveTitle: "儲存設定",
  },
  channelControl: "通道控制",
  channelTitle: "飛書通道",
  configSaved: "飛書設定已儲存",
  configSavedToast: "飛書配置已儲存",
  connectionFailed: "連線失敗",
  connectionOk: "連線成功",
  enable: "啟用飛書通道",
  encryptKey: "加密金鑰",
  encryptKeyDesc: "若飛書應用程式開啟了事件加密，填寫加密金鑰；否則留空",
  globalProxyNotConfigured: "全域代理未設定，無法同步",
  idle: {
    hint: "設定飛書應用憑證後即可連線，透過 WebSocket 長連線接收訊息，無需公網 IP",
  },
  info: "飛書資訊",
  intro: {
    line1: "透過飛書開放平台 WebSocket 長連線接收訊息，無需公網 IP。<br>",
    line2: "請在 <a href=\"https://open.feishu.cn/app\" target=\"_blank\" style=\"color:var(--accent)\">飛書開放平台</a> 建立自建應用程式，開啟機器人能力，申請 <code>im:message</code>、<code>im:message:send_as_bot</code> 權限，並在「事件訂閱」中選擇 WebSocket 長連線模式，訂閱 <code>im.message.receive_v1</code> 事件。",
  },
  label: {
    aiModel: "AI 模型",
    appId: "App ID",
    appSecret: "App Secret",
    appSecretSaved: "已儲存",
    encryptKey: "Encrypt Key",
    verifyToken: "Verify Token",
  },
  model: {
    default: "預設（跟隨全域設定）",
  },
  modelDefault: "預設（跟隨全域設定）",
  modelSaved: "飛書模型已儲存",
  notifyBound: "飛書通知已綁定到 {id}",
  notifyBoundToast: "飛書通知已綁定到 {id}",
  notifyCleared: "飛書通知已清除",
  notifyClearedToast: "飛書通知已清除",
  optionalSuffix: "（可選）",
  placeholder: {
    appSecret: "輸入應用金鑰",
    encryptKey: "開啟事件加密時填寫（可選）",
    encryptKeyShort: "開啟事件加密時填寫",
    saved: "已儲存（輸入新值以更新）",
    searchModel: "搜尋模型...",
    verifyToken: "事件驗證 Token（可選）",
  },
  proxy: {
    address: "代理位址",
    label: "代理",
    noAuthHint: "留空則無認證",
    password: "密碼",
    sectionTitle: "代理設定（企業網路可選）",
    syncGlobal: "同步全域代理",
    syncTitle: "從全域代理同步",
    title: "代理設定",
    username: "使用者名稱",
  },
  proxySaved: "代理設定已儲存",
  proxySyncedFromGlobal: "已從全域代理同步位址和使用者名稱，請輸入密碼後儲存",
  refreshStatus: "重新整理狀態",
  registerFailed: "註冊飛書失敗",
  runningHint: "飛書通道執行中，機器人已連線飛書伺服器，可接收訊息。",
  saveConfig: "儲存設定",
  saveConfigFailed: "儲存飛書設定失敗",
  saveFailed: "儲存失敗",
  saveFailedToast: "儲存失敗：{msg}",
  savingShort: "儲存中…",
  start: "啟動",
  startFailed: "飛書通道啟動失敗",
  startFailedFallback: "啟動失敗",
  startFailedToast: "飛書通道啟動失敗：{msg}",
  starting: "飛書通道啟動中…",
  startingToast: "飛書通道啟動中…",
  status: {
    connected: "飛書已連線，正在接收訊息",
    connecting: "正在連線飛書伺服器…",
    error: "通道出現錯誤",
  },
  statusText: {
    errorShort: "錯誤",
    running: "執行中",
    starting: "啟動中…",
    stopped: "已停止",
  },
  stop: "停止",
  stopChannelFailed: "停止飛書失敗",
  stopFailed: "停止失敗",
  stopFailedToast: "停止失敗：{msg}",
  stopped: "飛書通道已停止",
  stoppedToast: "飛書通道已停止",
  syncDisabled: "已關閉飛書同步",
  syncEnabled: "已開啟飛書同步",
  syncFailed: "同步失敗",
  syncSetFailed: "設定飛書同步失敗",
  testConnection: "測試連線",
  title: "飛書設定",
  verificationToken: "驗證 Token",
};

export default feishu;
