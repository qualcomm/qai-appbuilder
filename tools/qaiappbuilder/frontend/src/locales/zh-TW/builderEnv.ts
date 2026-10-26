// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工維護，UTF-8（無 BOM）。
// 命名空間：builderEnv —— 模型轉換環境（x64 Python 3.10 + QAIRT SDK）的下載頁
// 安裝卡片與聊天頁頂部提示。key 結構必須與 en 完全一致（tsc + parity 測試強制）。
// =============================================================================

const builderEnv = {
  title: "模型轉換環境",
  ready: "模型轉換環境已安裝，可以使用。",
  missing:
    "模型轉換環境（Python 3.10 + QAIRT SDK）尚未安裝。只有轉換模型時才需要它，其他功能不受影響。",
  missingParts: "缺少",
  versionMismatch: "目前設定指向 QAIRT SDK {installed}，此版本需要 {expected}。",
  sizeHint:
    "需要下載約 4–5 GB（QAIRT SDK 約 2 GB，Python 相依套件約 2.5 GB），支援斷點續傳，可隨時取消，之後接著下載。",
  unavailable: "目前版本無法自動安裝。",
  installing: "正在安裝模型轉換環境",
  keepUsing: "安裝期間可以繼續使用本應用程式。",
  failed: "安裝失敗",
  failedAt: "失敗步驟",
  cancelled: "安裝已取消。",
  resumeHint: "已下載的內容會保留，重試時會從中斷處繼續。",
  install: "安裝",
  retry: "重試",
  cancel: "取消",
  eta: "剩餘",
  options: "網路選項",
  mirror: "下載來源",
  mirrorAuto: "自動選擇（建議）",
  mirrorOfficial: "官方來源",
  mirrorCn: "中國大陸鏡像",
  proxy: "代理（選填）",
  proxyInvalid: "代理位址必須以 http://、https:// 或 socks5:// 開頭",
  log: "安裝輸出",
  step: {
    python310: "Python 3.10",
    venv: "虛擬環境",
    deps: "Python 相依套件",
    sdk_download: "下載 QAIRT SDK",
    sdk_extract: "解壓縮 QAIRT SDK",
    config: "寫入設定",
    verify: "驗證",
  },
  part: {
    config: "設定檔",
    sdk: "QAIRT SDK",
    venv: "Python 3.10 環境",
  },
  error: {
    network: "下載時網路發生錯誤。請檢查網路、下載來源或代理後重試。",
    disk_space: "磁碟可用空間不足。",
    uv_missing: "缺少 uv 工具，請先執行 setup_env.bat。",
    python_install: "無法安裝 Python 3.10。",
    venv: "無法建立 Python 3.10 環境。",
    deps: "安裝 Python 相依套件失敗。",
    extract: "無法解壓縮 QAIRT SDK。",
    permission: "沒有寫入 SDK 目錄的權限，請以系統管理員身分執行。",
    verify: "已安裝的環境未通過驗證。",
    already_running: "已有另一個安裝工作正在執行。",
    cancelled: "已取消。",
    spawn_failed: "無法啟動安裝程式。",
    unsupported_os: "只能在 Windows 上安裝。",
    internal: "安裝程式發生未預期的錯誤。",
  },
  banner: {
    missing: "模型轉換需要一個額外的環境，目前尚未安裝。",
    installing: "正在安裝模型轉換環境… {percent}%",
    failed: "模型轉換環境安裝失敗。",
    cancelled: "模型轉換環境的安裝已取消。",
    goInstall: "前往下載頁",
    viewProgress: "檢視進度",
  },
};

export default builderEnv;
