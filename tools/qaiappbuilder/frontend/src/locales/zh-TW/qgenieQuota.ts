// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工維護，UTF-8（無 BOM）。
//
// 結構必須與 en/qgenieQuota.ts 完全一致（key 一一對應），由 locale parity
// 測試 + tsc 強制。修改時嚴守 AGENTS.md §8 檔案編碼鐵律。
// =============================================================================

// QGenie 雙桶配額 —— 側邊欄用量條 + 配額耗盡提示。
//
// class.Api / class.UI 按 QGenie 的 wire 值命名，但顯示其控制台使用的標籤，
// 使使用者在此看到的與 QGenie 網頁端一致。
const qgenieQuota = {
  label: "QGenie 配額",
  class: {
    Api: "API/SDK",
    UI: "IDE/CLI",
  },
  inUse: "目前使用",
  daily: "當日",
  weekly: "本週",
  full: "已用盡",
  unknown: "未知",
  staleBadge: "快取",
  fetchedAt: "資料時間 {when}",
  staleAt: "快取於 {when}（重新整理失敗）",
  // 必須寫明：QGenie 無法按模型拆分花費（其 observe-only 寫入器不儲存模型
  // 識別）。不說明的話，使用者會誤以為這是目前模型的花費。
  costHeading: "QGenie 全部模型總花費（非僅目前模型）",
  costToday: "今日",
  costMonth: "本月",
  // 點擊用量條彈出的視窗：tooltip 只能描述目前模型，逐模型的當日資料放這裡。
  dialogTitle: "QGenie 當日配額",
  colModel: "模型",
  colClass: "配額類別",
  colUsedToday: "當日已用",
  colDailyLimit: "當日總量",
  selected: "目前",
  showIdle: "顯示 {count} 個未使用的模型",
  hideIdle: "隱藏未使用的模型",
  close: "關閉",
  // 階段式提醒：第一次跨線是建議性的（剩下的幾個百分點仍是數百萬 tokens），
  // 第二次是斷流前的最後一次機會。
  finalTitle: "{class} 配額即將耗盡",
  finalBody:
    "{model} 的當日 {class} 配額即將用盡，下一次回覆可能在中途被截斷。",
  keepGoing: "繼續使用",
  pickAnotherModel: "換一個模型",
  remainingTokens: "當日剩餘 {count}",
  clickToSwitch: "點擊後將後續請求計入 {class}",
  // 用量條無資料（或僅有舊資料）的原因。每條都指明對應的處理動作——
  // 單說「沒有資料」對使用者毫無幫助。
  reason: {
    no_api_key: "未設定 QGenie API Key —— 請在 設定 → 雲端模型 中填寫。",
    rate_limited:
      "QGenie 正在限流配額查詢（約每分鐘一次），顯示的數字可能略舊。",
    unreachable: "無法連線 QGenie，請檢查網路或 VPN。",
    not_configured: "此版本未設定 QGenie。",
    bad_base_url: "QGenie provider 的 base URL 不可用。",
    unknown: "無法讀取配額（{code}）。",
  },
  // 過濾與手動同步。
  filterPlaceholder: "過濾模型…",
  noMatch: "沒有符合「{query}」的模型。",
  sync: "同步",
  syncing: "同步中…",
  syncTooltip: "立即取得最新資料。QGenie 對此約限流為每分鐘一次。",
  switchTitle: "{class} 配額即將用盡",
  switchBody:
    "{model} 的當日 {class} 配額已使用 {percent}%。是否將後續請求切換到 {other}？",
  switchConfirm: "切換到 {other}",
  switchCancel: "繼續使用 {class}",
  switchedNotice: "已切換到 {other} 配額。",
  bothTitle: "QGenie 兩類配額均已用盡",
  bothBody:
    "{model} 的 API/SDK 與 IDE/CLI 當日配額都已用盡。請選擇其它模型——切換配額類別無法解決。",
  bothConfirm: "知道了",
  interruptedTitle: "回覆被中斷：配額已用盡",
  interruptedBody:
    "{model} 的當日 {class} 配額在回覆過程中用盡。上方已產生的內容會保留。可切換配額類別後重新提問，或改用其它模型。",
};

export default qgenieQuota;
