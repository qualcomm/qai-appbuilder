// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工維護，UTF-8（無 BOM）。
//
// chatErrors 命名空間 —— 宣告式聊天錯誤註冊表文案（標題、面向使用者的簡明訊息、
// 操作按鈕標籤）+ TLS 安全警告對話框。由 chatErrorActions.ts（訊息/標籤 key）與
// useChatErrorActions.ts（對話框 + toast key）消費。en / zh-CN / zh-TW 的 key
// 結構須完全一致（由 tsc + locale parity 測試強制）。
// =============================================================================

const chatErrors = {
  generic: "請求失敗。你可以重試，或複製診斷資訊以檢視詳情。",
  messages: {
    tlsCertUntrusted:
      "無法驗證模型服務的 TLS 憑證（可能是自簽或企業閘道憑證）。關閉驗證會降低安全性，請僅在你信任該服務時使用。",
    tlsHostnameMismatch: "憑證與服務位址不符，通常是 base_url 主機名有誤。",
    tlsCertExpired: "模型服務的 TLS 憑證已過期，請聯絡服務方或修正 base_url。",
    tlsHandshakeFailed: "與模型服務的 TLS 交握失敗，請檢查 base_url 和網路。",
    dnsError: "無法解析模型服務位址，請檢查 base_url、VPN 或網路。",
    connectionRefused:
      "模型服務拒絕連線，請確認服務已啟動、連接埠/base_url 正確。",
    hostUnreachable: "無法連通模型服務主機，請檢查 base_url 和網路。",
    networkExhausted: "網路長時間未恢復，已停止自動重連。",
    serverError: "模型服務暫時故障，已重試多次仍失敗。",
    authFailed: "認證失敗，API Key 無效或已過期。",
    permissionDenied: "無權存取該模型（可能未授權或區域限制）。",
    modelUnavailable: "找不到該模型或端點，請更換模型或檢查設定。",
    unsupportedParam:
      "該模型不支援某個取樣參數，請在雲端模型設定中關閉它。",
    promptTooLong: "提示詞超出模型的上下文視窗，請壓縮對話後重試。",
    throttling: "被模型服務限流，請稍後重試。",
    contentFiltered: "請求被模型的內容過濾攔截。",
  },
  actions: {
    disableTlsAndRetry: "僅信任並關閉驗證後重試",
    openProviderSettings: "開啟雲端模型設定",
    setApiKey: "設定 API Key",
    selectModel: "選擇模型",
    switchModel: "換個模型重試",
    compressContext: "壓縮上下文",
    copyDiagnostics: "複製診斷資訊",
  },
  tlsWarning: {
    title: "關閉 TLS 驗證？",
    message:
      "關閉後將不再驗證伺服器憑證，存在中間人攻擊風險。僅在你確認該服務可信時繼續。",
    confirm: "關閉並重試",
    cancel: "取消",
    disabledToast: "已關閉 TLS 驗證，正在重試…",
  },
  compressHint: "在聊天中傳送 /compact 壓縮對話歷史，然後重試。",
  diagnosticsCopied: "診斷資訊已複製到剪貼簿",
  diagnosticsCopyFailed: "無法複製到剪貼簿",
};

export default chatErrors;
