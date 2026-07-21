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

const channels = {
  callbackUrl: "回呼網址",
  configure: "設定",
  connect: "連接",
  connected: "已連接",
  copied: "回呼網址已複製",
  autoStart: "服務啟動時自動開啟此通道",
  autoStartSaveFailed: "更新自動開啟設定失敗",
  configSaved: "飛書設定已儲存",
  configSaveFailed: "儲存飛書設定失敗",
  settingsSaved: "通道設定已儲存",
  settingsSaveFailed: "儲存通道設定失敗",
  settingsLoadFailed: "載入通道設定失敗",
  legacy_notice: "此檢視使用的是舊版元件路徑，請更新到 FeishuConfigPanel。",
  modelLabel: "AI 模型",
  modelFollowGlobal: "預設（跟隨全域設定）",
  modelSearch: "搜尋模型…",
  modelNoCandidates: "尚未設定雲端模型",
  modelSaved: "通道模型已儲存",
  modelSaveFailed: "儲存通道模型失敗",
  proxyTitle: "代理設定",
  proxyLabel: "代理",
  proxyAddress: "代理位址",
  proxyAddressPlaceholder: "http://proxy:8080",
  proxyUsername: "使用者名稱",
  proxyUsernamePlaceholder: "（選填）",
  proxyPassword: "密碼",
  proxyPasswordPlaceholder: "（不修改）",
  proxySyncGlobal: "同步全域代理",
  proxySynced: "已從全域代理同步（請記得儲存）",
  proxySaved: "代理設定已儲存",
  proxySaveFailed: "儲存代理設定失敗",
  settings_btn: "設定",
  start: "啟動",
  starting: "正在啟動飛書通道…",
  started: "飛書通道已啟動。",
  stop: "停止",
  stopping: "正在停止飛書通道…",
  stopped: "飛書通道已停止。",
  disableChannel: "停用通道",
  disconnect: "中斷連接",
  disconnected: "未連接",
  enableChannel: "啟用通道",
  scan_wechat: "用手機微信掃描上方二維碼",
  qr_scanned: "已掃碼，請在手機上確認",
  qr_expired: "二維碼已過期",
  qr_countdown: "{seconds}s 後自動重新整理",
  qr_refresh: "重新整理二維碼",
  qr_reget: "重新取得二維碼",
  feishu: {
    cardDesc: "透過飛書開放平台 WebSocket 長連線接入，無需公網 IP",
    connect: "連接飛書",
    connectedMsg: "飛書已連線，正在接收訊息",
    connectingMsg: "正在連接飛書伺服器…",
    errorMsg: "通道出錯",
    idleHint: "填寫飛書應用憑證以透過 WebSocket 長連線接入，無需公網 IP",
    introLine1: "透過飛書開放平台 WebSocket 長連線接收訊息，無需公網 IP。<br>",
    // introLine2 拆分：把「飛書開放平台」外連從段落中抽出，改為獨立成行的按鈕式外連。
    // 段落僅保留敘述文字（無 <a>），按鈕由 FeishuConfigPanel 模板自行組合。
    introLine2Prefix: "請在飛書開放平台建立自建應用，開啟機器人能力，申請 <code>im:message</code>、<code>im:message:send_as_bot</code> 權限，並在「事件訂閱」中選擇 WebSocket 長連線模式，訂閱 <code>im.message.receive_v1</code> 事件。",
    openPlatformLabel: "開啟飛書開放平台",
    openPlatformTooltip: "跳轉到飛書開放平台",
    refreshStatus: "重新整理狀態",
    runningHint: "飛書通道運行中，機器人已連接飛書伺服器，可接收訊息。",
    info: {
      btnTitle: "飛書通道說明",
      subtitle: "飛書開放平台 · WebSocket 長連線",
      title: "飛書通道說明",
      stepsTitle: "⚙️ 配置步驟",
      step1: "在 <a href=\"https://open.feishu.cn/app\" target=\"_blank\" style=\"color:var(--accent)\">飛書開放平台</a> 建立自建應用",
      step2: "開啟<strong>機器人</strong>能力",
      step3: "申請 <code>im:message</code>、<code>im:message:send_as_bot</code> 權限",
      step4: "在「事件訂閱」中選擇 <strong>WebSocket 長連線</strong>模式",
      step5: "訂閱 <code>im.message.receive_v1</code> 事件",
      step6: "將 App ID 和 App Secret 填入配置並儲存",
      advantagesTitle: "✅ 優勢",
      adv1: "<strong>無需公網 IP</strong>：透過 WebSocket 長連線主動接收訊息，無需配置伺服器",
      adv2: "<strong>無需掃碼</strong>：使用應用憑證（App ID + Secret）直接認證，重啟自動重連",
      adv3: "<strong>支援圖片</strong>：AI 可自動識別並描述飛書訊息中的圖片內容",
      adv4: "<strong>自動切換模型</strong>：本地模型不可用時自動切換到雲端模型並提前通知",
      notesTitle: "⚠️ 注意事項",
      note1: "每位飛書使用者的對話歷史<strong>獨立儲存</strong>，可在 Chat 介面檢視歷史記錄",
      note2: "若企業網路有代理，請在「代理設定」中配置代理位址",
      note3: "App Secret 為敏感欄位，儲存後顯示為 ****，不會明文儲存",
    },
    name: "飛書",
    status: {
      error: "出錯",
      running: "已連線",
      starting: "連線中…",
      stopped: "未連線",
    },
  },
  feishuDesc: "連接飛書機器人訊息",
  guideBtn: "使用指南",
  settingsBtn: "通道設定",
  guide: {
    subtitle: "微信 · 飛書 · 通用指令",
    title: "機器人通道使用說明",
    smartTitle: "🔄 智慧模型切換",
    smart1: "當通道配置的模型為<strong>本地模型</strong>（或跟隨全域設定且全域選擇的是本地模型）時，若本地模型服務<strong>未啟動或不可用</strong>，系統會自動偵測並嘗試回退。",
    smart2: "若偵測到有<strong>可用的雲端模型</strong>，系統會在回覆前先發送一條提示：<br><em style=\"color:var(--text-secondary)\">⚠️ 本地模型目前不可用，已自動切換到雲端模型：xxx</em>",
    smart3: "若<strong>沒有配置任何雲端模型</strong>，則仍會嘗試路由到本地服務，此時訊息可能會失敗。建議提前配置至少一個雲端模型作為備選。",
    smart4: "本地模型恢復後，可發送 <strong>/model 0</strong> 恢復跟隨全域設定。",
  },
  cmd: {
    sectionTitle: "⌨️ 普通對話指令",
    help: "顯示完整指令說明",
    new: "儲存目前會話後開啟新會話",
    clear: "刪除目前會話歷史（不儲存）後開啟新會話",
    list: "檢視最近 N 條歷史會話（預設 5 條）",
    use: "切換到指定歷史會話",
    status: "檢視目前會話狀態（名稱、對話輪數）",
    rename: "重新命名目前會話",
    delete: "刪除目前會話（不可恢復），並開啟新會話",
    stop: "立即停止目前正在執行的任務",
    models: "檢視可用模型列表",
    model: "檢視/切換模型；/model 0 恢復全域設定",
    compact: "檢視/暫時修改目前會話歷史輪次",
    reboot: "重啟整個服務，重啟後微信和飛書通道將自動重連",
    rebootWechat: "重啟服務，重啟後微信通道自動重連",
    rebootFeishu: "重啟服務，重啟後飛書通道自動重連",
    helpFull:
      "顯示此使用說明（所有可用指令清單）。<br><em>回覆：完整的指令說明資訊</em>",
    newFull:
      "<strong>儲存</strong>目前會話歷史後開啟新會話，歷史記錄保留在 Chat 介面可檢視。<br><em>回覆：已開啟新會話 ✨</em>",
    clearFull:
      "<strong>刪除</strong>目前會話歷史（不儲存）後開啟新會話，Chat 介面中該記錄將被永久移除。<br><em>回覆：目前會話已清除 🗑</em>",
    listFull: "檢視最近 N 條歷史會話（預設 5 條），顯示名稱、時間和對話輪數。",
    useFull: "切換到指定編號的歷史會話繼續對話。",
    statusFull: "檢視目前會話狀態（名稱、對話輪數、上下文大小）。",
    renameFull: "重新命名目前會話。",
    deleteFull: "刪除目前會話（不可復原），並開啟新會話。",
    stopFull:
      "<strong>立即停止</strong>目前正在執行的任務（普通對話或 Claude Code 任務均支援）。<br><em>回覆：⏹️ 目前任務已停止，可以傳送新訊息繼續。</em>",
    modelsFull:
      "檢視目前所有<strong>可用模型清單</strong>（本機 + 雲端），並顯示目前正在使用的模型。<br><em>回覆：帶編號的模型清單</em>",
    modelFull:
      "按 <strong>/models</strong> 清單中的編號<strong>切換模型</strong>，也可直接輸入 model_id。傳送 <strong>/model 0</strong> 恢復跟隨全域設定。<br><em>回覆：✅ 已切換到模型：xxx</em>",
    compactFull:
      "檢視或<strong>暫時修改</strong>目前會話保留的歷史輪次。<br><em>回覆：✅ 目前會話歷史輪次已設為 n 輪</em>",
    rebootFull:
      "重新啟動整個 QAIModelBuilder 服務，重新啟動完成後微信和飛書通道將自動重連。<br><em>回覆：系統正在重新啟動，請稍候... 🔄</em>",
  },
  help: "說明",
  settings: {
    history: {
      desc: "飛書 + 微信通道共用。超出後自動刪除最舊的完整輪次。",
      hint: "飛書/微信通道保留的最大對話輪次。",
      label: "對話歷史輪次",
      unit: "輪",
    },
    subtitle: "飛書 · 微信通道共用參數",
    title: "通道公共設定",
  },
  status: "狀態",
  subtitle: "管理訊息通道，連接外部平台",
  title: "通道",
  wechat: {
    cardDesc: "透過 iLink Bot 接入個人微信，收發文字與圖片訊息",
    info: {
      btnTitle: "微信通道說明",
      subtitle: "iLink Bot · 個人微信接入",
      title: "微信通道說明",
      notesTitle: "⚠️ 注意事項",
      note1: "本通道透過 <strong>iLink Bot</strong> 接入個人微信，需在手機上掃碼授權。",
      note2: "憑證儲存在本地，重啟服務後通常可<strong>自動重連</strong>，無需重複掃碼。",
      note3: "若長時間未使用或微信帳號在其他裝置登入，憑證可能失效，需重新掃碼。",
      note4: "每位微信使用者的對話歷史<strong>獨立儲存</strong>，可在 Chat 介面檢視歷史記錄。",
      note5: "支援發送<strong>圖片訊息</strong>，AI 將自動識別並描述圖片內容。",
    },
    name: "微信（個人號）",
    connect: "連接微信",
    rescan: "重新掃碼登入",
    rescanTitle: "強制重新掃碼登入（忽略已儲存憑證）",
    connectedMsg: "微信已連線，正在接收訊息",
    refreshStatus: "重新整理狀態",
    runningHint: "微信通道執行中，機器人已連線，可接收訊息。",
    errorMsg: "通道發生錯誤",
    idleHint: "用手機微信掃碼即可連接，開始收發訊息",
    status: {
      connected: "已連線",
      error: "出錯",
      expired: "已過期",
      idle: "未連線",
      logging_in: "等待掃碼",
      scanned: "已掃碼",
    },
  },
  wechatDesc: "連接企業微信機器人訊息",
};

export default channels;
