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

const voiceInput = {
  cancel: "取消",
  cancelTitle: "點擊取消轉寫",
  coldBadge: "○ 待載入",
  coldTooltip: "首次使用需載入模型（約 10-15 秒）",
  disabled: "語音輸入未啟用。請點擊工具列中的語音引擎選擇器以啟用。",
  dismiss: "關閉",
  empty: "未擷取到音訊，請檢查麥克風後重試。",
  encodeFailed: "音訊編碼失敗",
  engine: {
    whisper: "Whisper（多語言）",
    zipformer: "Zipformer（中文）",
  },
  engineLabel: "引擎",
  engineMenuHint: "ⓘ 提示：首次使用需載入模型（約 10-15 秒），之後一直可用；閒置 10 分鐘會自動釋放",
  engineMenuTitle: "語音輸入引擎",
  errorTitle: "語音輸入失敗",
  inferError: "語音模型「{model}」推理失敗：{detail}。可在引擎選單切到 {other}，或在 AppBuilder 重新下載該模型。",
  inferenceFailed: "語音轉寫失敗",
  listening: "正在聆聽…",
  loadingBadge: "◐ 載入中",
  loadingTooltip: "模型正在背景載入中",
  noTranscript: "未辨識到語音，請清晰地再說一次。",
  permissionDenied: "麥克風權限被拒絕，請在瀏覽器設定中允許使用麥克風。",
  phase: {
    preparing: "載入模型…",
    processing: "處理中…",
    queued: "排隊中…",
    running: "轉寫中…",
    uploading: "上傳中…",
  },
  popoverAria: "語音輸入控件",
  preloadingHint: "模型正在背景載入，請繼續說話",
  processingTitle: "正在轉寫…使用 ✕ 取消",
  retry: "重試",
  startAria: "開始語音輸入",
  startFailed: "麥克風啟動失敗",
  startTitle: "點擊開始語音輸入",
  stopAndTranscribe: "停止並轉寫",
  stopAria: "停止語音輸入",
  stopTitle: "點擊停止並轉寫",
  transcribing: "正在轉寫語音輸入…",
  unsupported: "語音輸入僅在桌面版 Chrome / Edge 可用。",
  unsupportedTitle: "語音輸入僅在桌面版 Chrome / Edge 可用",
  uploadFailed: "音訊上傳失敗",
  warmBadge: "● 已就緒",
  warmTooltip: "模型已載入，轉寫會很快",
  weightsMissing: "語音模型權重未安裝，請先在 AppBuilder 中下載 {model}。",
};

export default voiceInput;
