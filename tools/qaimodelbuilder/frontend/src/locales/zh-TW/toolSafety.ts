// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工維護，UTF-8（無 BOM）。
//
// 真值源說明：本項目 i18n 已無自動生成管道（舊的 _L8-locale-gen.py 與
// _migrated/*.json 均未保留在倉庫）。因此本檔案就是當前唯一真值源，
// 必須手工維護。修改時嚴守 AGENTS.md §3.10 檔案編碼鐵律（UTF-8，禁止
// GBK/CP437 等非 UTF-8 編碼，禁止雙重編碼損壞）。
//
// toolSafety — 工具防護 / Tool Safety 面板（2026-06 安全設定統一治理）。
// =============================================================================

const toolSafety = {
  title: "工具防護",
  subtitle: "為大模型工具呼叫提供兩層防護。",
  // ── 第 1 層 — 純軟體工具防護（熱生效）──
  layer1Title: "工具防護（始終可用）",
  layer1Desc:
    "對每次工具呼叫施加的純軟體准入防護。改動立即生效。",
  fileBrokerEnabled: "啟用 File Broker",
  fileBrokerDesc:
    "敏感路徑排除、危險寫入/執行攔截，以及 glob/grep 結果截斷。",
  fileBrokerRebootHint: "切換 File Broker 需要重啟以重建工具橋。",
  maxEntries: "最大條目數（glob/grep）",
  projectSkipDirs: "專案跳過目錄",
  projectSkipDirsDesc: "在專案級工具掃描中排除的目錄名。",
  projectSkipDirsPlaceholder: "node_modules",
  globalProxy: "全域代理",
  globalProxyDesc: "網路工具使用的 HTTP(S) 代理位址。留空表示不使用。",
  globalProxyPlaceholder: "http://proxy.example:8080",
  // ── 第 2 層 — PolicyCenter FileGuard ──
  layer2Title: "策略守衛（FileGuard）",
  layer2Desc:
    "由 PolicyCenter 依據當前策略對讀 / 寫 / 執行權限進行強制校驗。改動需要重啟。",
  fileGuardEnabled: "啟用 FileGuard",
  fileGuardDesc: "對工具的讀 / 寫 / 執行權限進行強制校驗，並同時守護子行程發起的檔案存取。",
  allowExecTool: "允許 exec 工具",
  allowExecToolDesc: "關閉後，exec 工具會在任何 broker 校驗前被直接拒絕。",
  // ── 始終開啟的安全底線（3c 開關樹 §6.4）——不可關的基線防護 ──
  alwaysOn: {
    title: "始終開啟的底線",
    desc: "無法關閉的基線防護。它們不讀取安全總閘，即使處於寬鬆（permissive）或關閉（disabled）模式下也始終強制生效。",
    banner: "這些底線始終開啟，無法從介面上關閉。",
    badge: "始終開啟",
    lockedTitle: "該防護始終開啟，無法關閉。",
    protectedPathsLabel: "受保護的系統路徑",
    protectedPathsDesc: "對關鍵系統位置的寫入始終被攔截，不受任何策略或授權影響。",
    dangerousBuiltinsLabel: "危險命令底線",
    dangerousBuiltinsDesc: "內建的破壞性命令清單始終需要顯式批准，永遠不會被靜默放行。",
    mainProcessHookLabel: "主行程稽核鉤子",
    mainProcessHookDesc: "主行程及其子行程始終被稽核；稽核哨兵不讀取總閘開關。",
  },
  // ── 第 3 層（OS 隔離沙箱）已於 2026-07-01 與 Persistent ACL 一併移除 ──
  // ── 自訂危險命令模式（P-10，僅增不刪的追加層）──
  dangerousCommands: {
    title: "自訂危險命令模式",
    desc: "在始終生效的內建底線之上，追加攔截破壞性命令的自訂正規表示式。自訂模式只能新增攔截範圍，絕不會移除內建防護。",
    builtinBanner: "內建底線模式始終強制生效，不可移除。",
    builtinLabel: "內建底線（唯讀）",
    builtinLockedTitle: "該內建模式始終強制生效，不可移除。",
    extraLabel: "自訂模式",
    extraDesc: "命令執行前會用這些正規表示式（不區分大小寫）進行比對。",
    extraPlaceholder: "例如 \\bshutdown\\b",
    rebootHint: "自訂模式在啟動時套用，儲存後需重啟才能生效。",
    save: "儲存模式",
    invalidPatterns: "以下模式不是合法的正規表示式，已被捨棄：{patterns}",
  },
  // ── 工具輸出上限（建置期 → 需重啟）──
  outputLimitsTitle: "工具輸出上限",
  outputLimitsDesc:
    "限制每個工具回傳給模型的結果體量。值越大，模型獲得的上下文越多，但消耗的 token 也越多。被截斷的內容會落盤，可用 read 工具取回。改動其中任意一項都需要重啟才能生效。",
  readMaxLines: "read — 最大行數",
  readMaxLinesDesc:
    "read 工具單次回傳的最大行數；超出部分會被截斷。",
  readMaxBytes: "read — 最大位元組數",
  readMaxBytesDesc:
    "read 工具單次回傳的最大位元組數；更大的檔案會被截斷。",
  readMaxLineLength: "read — 單行最大長度",
  readMaxLineLengthDesc:
    "read 回傳時，超過該字元數的行會被截斷。",
  globMaxResults: "glob — 最大結果數",
  globMaxResultsDesc:
    "glob 工具一次最多給模型展示多少個檔案路徑；超出部分會落盤，可用 read 工具取回。",
  grepMaxMatches: "grep — 最大匹配數",
  grepMaxMatchesDesc:
    "grep 工具一次最多給模型展示多少條匹配；超出部分會落盤，可用 read 工具取回。",
  grepMaxLineLength: "grep — 單行最大長度",
  grepMaxLineLengthDesc:
    "grep 回傳匹配時，超過該字元數的行會被截斷。",
  grepMaxOutputBytes: "grep — 最大輸出位元組數",
  grepMaxOutputBytesDesc:
    "grep 單次交給模型的輸出總位元組上限；超出部分會被截斷。",
  // ── 重啟確認對話框（決策 3B）──
  rebootTitle: "需要重啟",
  rebootMessage:
    "已修改並儲存安全相關配置，但需要重啟才能生效。是否立即重啟？",
  rebootConfirm: "立即重啟",
  rebootCancel: "稍後",
  rebootDeferred: "已儲存。該改動將在下次重啟後生效。",
  // ── 狀態 ──
  saved: "已儲存",
  saveFailed: "儲存失敗",
  add: "新增",
  remove: "移除",
};

export default toolSafety;
