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

const appConfig = {
  agentLoopTitle: "Agent 迴圈",
  aiCodingDesc: "控制聊天輸入框上方 Claude Code / Open Code 模式按鈕的顯隱。關閉後對應按鈕從工具列隱藏，後端工作階段保留不受影響。",
  aiCodingTitle: "AI 程式設計助手",
  allowExecDesc: "啟用後，AI 助手可透過 <code>exec</code> 工具執行系統指令（執行腳本、呼叫 CLI 等）。<br>當 <b>綁定地址為 0.0.0.0</b> 時，關閉此選項可防止區域網路裝置透過 API 觸發任意指令執行。暴露到區域網路時建議關閉。",
  allowExecLabel: "允許執行工具",
  sslVerifyLabel: "校驗 TLS/SSL 憑證",
  sslVerifyDesc: "開啟後，所有出站 HTTPS 連線（模型服務、webfetch、MCP、登入）都會校驗伺服器的 TLS 憑證。若你的模型服務使用自簽名憑證或企業閘道憑證，請<b>關閉</b>此項。修改對 webfetch 工具立即生效；對模型服務連線需重啟後完全生效。",
  sslVerifyRebootTitle: "是否重啟以生效？",
  sslVerifyRebootMessage: "TLS 校驗的修改需重啟後才能對模型服務及其他連線完全生效。現在重啟嗎？",
  sslVerifyRebootConfirm: "立即重啟",
  sslVerifyRebootCancel: "稍後",
  sslVerifyRebootDeferred: "已儲存。稍後重啟以完全生效。",
  sslVerifySaved: "TLS 校驗設定已儲存。",
  autoCompressDesc: "接近 token 上限時自動壓縮上下文",
  autoCompressLabel: "自動壓縮",
  autoSave: "修改時自動儲存",
  autoTitleDesc: "自動生成對話標題",
  autoTitleLabel: "自動標題",
  bindAddressChangedToast: "⚠️ 綁定地址已修改，請重新啟動 QAIModelBuilder 使其生效。",
  bindAddressDesc: "控制 QAIModelBuilder 監聽的網路介面。<br><b>127.0.0.1</b>（推薦）：僅本機可存取，最安全。<br><b>0.0.0.0</b>（除錯/區域網路模式）：區域網路內所有裝置均可存取，會將所有 API 端點（含檔案讀寫和 exec 工具）暴露給區域網路裝置，僅在可信任網路中使用。<b>修改後需重新啟動才能生效。</b>",
  bindAddressLabel: "WebUI 繫結位址",
  bindAll: "0.0.0.0 — 所有介面，區域網路可存取（偵錯模式）",
  bindHostDesc: "控制 QAIModelBuilder 監聽的網路介面。<br><b>127.0.0.1</b>（推薦）：僅本機可存取，最安全。<br><b>0.0.0.0</b>（除錯/區域網路模式）：區域網路內所有裝置均可存取，會將所有 API 端點（含檔案讀寫和 exec 工具）暴露給區域網路裝置，僅在可信任網路中使用。<b>修改後需重新啟動才能生效。</b>",
  bindHostLabel: "WebUI 繫結位址",
  bindLocalOnly: "127.0.0.1 — 僅本機存取（推薦）",
  channelsTitle: "通道",
  chatDisplayTitle: "對話顯示",
  chatDisplayDesc: "控制對話區域中訊息的渲染方式。這些偏好會在重啟後保留。",
  showToolCallsLabel: "顯示工具呼叫卡片",
  showToolCallsDesc: "啟用後，工具呼叫卡片（參數 + 輸出）會內嵌顯示在助理回覆中。<b>關閉</b>後僅隱藏對話視圖中的卡片顯示——工具呼叫照常執行、歷史記錄照常保留。大多數使用者保持啟用；希望對話視圖更簡潔時可關閉。",
  compactionProtectDesc: "最近的對話中有多少（佔模型視窗的百分比）始終保留原文、永不壓縮。預設 35%。",
  compactionProtectLabel: "最近對話保護大小",
  compactionTargetDesc: "壓縮後，把上下文縮減到模型視窗的百分之幾。越低壓縮越積極（例如 35% 會把 200K 視窗壓到約 70K）。預設 35%。",
  compactionTargetLabel: "壓縮後保留大小",
  debugTitle: "除錯",
  appBuilderTitle: "應用建構器",
  appBuilderDesc: "應用建構器設定。預設進入應用建構器模式時不會開啟較重的模型工作台——你在工具列中選擇已匯入的模型，由 Agent 幫你建構 WebUI 應用。",
  showWorkbench: "顯示模型工作台",
  showWorkbenchDesc: "開啟後，進入應用建構器模式會開啟完整的模型試用/執行工作台（執行、效能、歷史、比較）。預設關閉；無論開關與否，工作台及其全部功能都保留。",
  modeIntroTitle: "模式引導提示",
  modeIntroDesc: "App Builder / GoMaster / Model Builder 模式內的引導卡片可被勾選「不再顯示」後永久關閉。在此處重新開啟開關即可恢復顯示。",
  modeIntroAppBuilder: "顯示 App Builder 引導",
  modeIntroGomaster: "顯示 GoMaster 引導",
  modeIntroModelBuilder: "顯示 Model Builder 引導",
  modeIntroModelHub: "顯示 Model Hub 引導",
  modeIntroPro: "顯示增強模式引導",
  modeIntroCode: "顯示程式碼模式引導",
  enableCCInToolbar: "在工具列中啟用 Claude Code",
  enableCCInToolbarDesc: "啟用後，聊天輸入框上方顯示 Claude Code 按鈕（🤖）。點擊進入 Claude Code 模式；右鍵退出。",
  enableOCInToolbar: "在工具列中啟用 Open Code",
  enableOCInToolbarDesc: "啟用後，聊天輸入框上方顯示 Open Code 按鈕（🔷）。點擊進入 Open Code 模式；右鍵退出。",
  experienceExtractionDesc: "任務成功後自動提取可複用經驗",
  experienceExtractionLabel: "經驗沉澱",
  forgeConfigPath: "設定檔路徑",
  lanWarning: "⚠️ <b>已啟用區域網路存取。</b>區域網路內任何裝置均可存取 WebUI 及所有 API 端點，包括檔案讀寫和 exec 工具。請僅在可信任網路中使用。建議同時關閉下方的 <b>Allow Exec Tool</b> 以降低風險。",
  logBufferDesc: "服務日誌緩衝區大小（行數）。後端最多保留此行數的日誌，前端 UI 也以此為上限顯示。<br>修改後下次啟動 GenieAPIService 時生效。建議範圍：1000～20000。",
  logBufferHint: "預設值：6000 行。",
  logBufferLabel: "服務日誌緩衝區大小",
  maxHistoryRoundsDesc: "WebUI Chat、微信通道、飛書通道共用的對話歷史保留輪次上限。<br>一輪 = 一條使用者訊息 + 期間所有工具呼叫 + 最終 AI 回覆。<br>超過此輪次的舊訊息將在每次對話後自動從記憶體中移除（不影響已持久化的歷史記錄）。<br>WebUI Chat 可透過 <code>/compact &lt;輪次&gt;</code> 指令手動裁剪目前對話歷史。",
  maxHistoryRoundsHint: "預設值：20 輪。建議範圍：5～50 輪。值越大，上下文越豐富，但 prompt 越長。",
  maxHistoryRoundsLabel: "最大歷史輪次",
  maxIterationsDesc: "Agentic loop 最大工具調用輪次",
  maxIterationsLabel: "最大迭代次數",
  orderLabel: "順序：",
  proxyDesc: "適用於版本檢查、模型目錄下載、雲端 AI 模型 API 等所有網路請求。<br>代理密碼透過系統安全儲存保存，不寫入設定檔。",
  proxyPassword: "密碼",
  proxyPasswordPlaceholder: "留空則無驗證（顯示 **** 表示已設定密碼）",
  proxySaveBtn: "儲存代理設定",
  proxySaving: "儲存中…",
  proxyTitle: "網路代理",
  proxyUrl: "代理位址",
  proxyUrlPlaceholder: "http://proxy.company.com:8080（留空則不使用代理）",
  proxyUsername: "使用者名稱",
  proxyUsernamePlaceholder: "留空則無驗證",
  resetBtn: "重設",
  saveBtn: "儲存設定",
  savingBtn: "儲存中…",
  securityTitle: "安全",
  showPromptDesc: "開啟後，每次 AI 回覆訊息旁顯示剪貼簿按鈕，點擊可檢視本次請求傳送給模型的完整提示詞（含系統提示詞和歷史訊息）。快照僅保存在記憶體中，重新啟動服務後清空。",
  showPromptLabel: "在 UI 中顯示提示詞",
  title: "應用程式設定",
  toolbarModulesDesc: "控制聊天輸入框底部快捷模組按鈕的顯隱。關閉某模組後，對應按鈕將從工具列中隱藏（已建立的工作階段不受影響）。",
  toolbarModulesTitle: "工具列模組",
  workspaceModelRootDesc: "模型轉換產物的存放根目錄，預設 C:\\WoS_AI。",
  workspaceModelRootLabel: "模型工作區目錄",
  workspaceModelRootPlaceholder: "C:\\WoS_AI",
  workspaceTitle: "工作區",
};

export default appConfig;
