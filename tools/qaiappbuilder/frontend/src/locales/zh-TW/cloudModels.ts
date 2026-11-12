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

const cloudModels = {
  add: "新增模型",
  addFirst: "新增第一個雲端模型開始使用",
  apiKey: "API 金鑰",
  apiKeyKeepHint: "已儲存的金鑰不回顯，留空保持不變",
  apiKeyOptional: "API Key（可選）",
  baseUrlOptional: "Base URL（可選）",
  clone: "複製",
  confirmDelete: "確定要刪除此雲端模型嗎？",
  confirmDeleteMsg: "確定要刪除 \"{name}\"（{model_id}）嗎？此操作無法撤銷。",
  contextLength: "上下文長度 (tokens)",
  delete: "刪除模型",
  deleteFailed: "刪除失敗",
  deleted: "雲端模型已刪除",
  description: "描述",
  displayNameRequired: "Display Name 不能為空",
  edit: "編輯模型",
  endpoint: "介面網址",
  hide: "隱藏",
  isDefault: "設為預設",
  // 替代 keyless（auth_mode: "sso"）提供商的 API Key 輸入框 —— QAI Service
  // 令牌池用登入使用者的短期權杖鑑權，不需要使用者填寫金鑰。
  keylessProviderHint: "使用登入帳號，無需 API Key",
  loadFailed: "載入雲端模型失敗",
  modelAdded: "模型已新增",
  modelId: "模型 ID",
  modelIdDesc: "在模型選擇器中顯示的 id。未設定「API 模型 ID」覆寫時原樣傳送給 API。",
  apiModelId: "API 模型 ID（選填）",
  apiModelIdDesc: "當與「模型 ID」不同時，實際傳送給 Provider API 的上游模型名（如帶日期的 \"claude-sonnet-4-20250514\"）。留空則使用「模型 ID」。",
  modelIdDuplicate: "該 Provider 下 Model ID \"{model_id}\" 已存在",
  modelIdRequired: "Model ID 不能為空",
  modelUpdated: "模型已更新",
  name: "顯示名稱",
  noModels: "暫無雲端模型設定",
  onboarding: {
    title: "尚未設定雲端模型",
    desc: "在設定中新增雲端模型即可使用雲端服務商（如 Claude、GPT）對話。未設定時仍可使用本機裝置模型。",
    cta: "前往設定",
  },
  apiKeyOnboarding: {
    title: "設定你的 API 金鑰",
    desc: "雲端模型已為你預先設定完成，只需填入 API 金鑰即可開始使用。",
    cta: "設定 API 金鑰",
  },
  apiKeyDialog: {
    title: "設定 API 金鑰",
    subtitle: "此金鑰將套用於所有預設定的雲端模型。",
    placeholder: "請輸入你的 API 金鑰",
    save: "儲存",
    cancel: "取消",
    saveSuccess: "API 金鑰已儲存",
    saveError: "儲存 API 金鑰失敗",
  },
  apiKeyError: {
    message: "此雲端模型需要先設定 API 金鑰才能使用。",
    setKeyCta: "設定 API 金鑰",
    goToSettingsCta: "雲端模型設定",
  },
  pinProvider: "置頂此 Provider",
  pinned: "已置頂",
  provider: "供應商",
  providerApiKey: "供應商 API 金鑰",
  providerApiKeyDesc: "此 Provider 下所有模型共用，優先級高於全域設定。",
  providerBaseUrl: "供應商接入地址",
  providerBaseUrlDesc: "OpenAI API 相容地址，此 Provider 下所有模型共用。",
  providerFieldSaved: "已儲存",
  providerRequired: "Provider 不能為空",
  save: "儲存",
  saveFailed: "儲存失敗",
  saved: "雲端模型已儲存",
  search: "搜尋雲端模型...",
  show: "顯示",
  supportsStreaming: "支援串流輸出",
  paramsTitle: "支援的參數",
  paramsDesc:
    "若該模型不支援某個參數（傳送會導致請求失敗），請將其關閉。聊天取樣面板仍會顯示對應控制項，但不支援的參數會在請求傳送前被自動剔除。",
  paramThoughtSigHint:
    "僅在需要回傳思考簽章的 Vertex AI 思考模型（如 Gemini）上開啟。",
  gateway: {
    title: "企業閘道（QAI Service）",
    desc: "選用功能，僅適用於擁有公司 QAI Service 位址的使用者。模型由閘道在您使用公司 SSO 登入後提供；在您儲存位址並確認之前，不會啟用任何內容。",
    urlPlaceholder: "https://qai-service.example.com:8012",
    urlInvalid: "請輸入以 http:// 或 https:// 開頭的完整位址",
    emptyHint: "如需關閉，請清空位址後點擊「中斷連線」。",
    saveBtn: "儲存並連線",
    retryBtn: "重新登入並同步",
    disconnectBtn: "中斷連線",
    stateOff: "未連線",
    stateNeedLogin: "位址已儲存，需要登入",
    stateOn: "已連線，{n} 個模型",
    phase: {
      saving: "正在儲存…",
      login: "正在透過公司 SSO 登入…",
      syncing: "正在同步模型…",
    },
    confirmTitle: "連線到 QAI Service？",
    confirmMsg:
      "位址：{url}\n\n接下來會發生：\n1. 位址儲存在本機。\n2. 需要再用公司 SSO 登入一次（可能會彈出視窗）。\n3. 從閘道取得模型清單，並顯示在模型選擇器中。\n\n如有失敗，可再次點擊按鈕重試。",
    confirmBtn: "儲存並連線",
    disconnectTitle: "中斷 QAI Service 連線？",
    disconnectMsg:
      "將從本機移除已儲存的位址、閘道提供的模型以及對應的登入權杖。公司 SSO 登入本身不受影響。",
    connected: "QAI Service 已連線（{n} 個模型）",
    errSave: "儲存位址失敗，請重試。",
    errLogin: "登入未完成（視窗被關閉或被攔截）。位址已儲存，點擊按鈕可重試。",
    errNoToken: "登入已完成，但閘道沒有簽發權杖。請檢查位址與帳號後重試。",
    errSyncFailed: "無法從閘道取得模型清單。請檢查位址與網路後重試。",
  },
  quick: {
    title: "新增雲端模型",
    step1: "1. 選擇服務商",
    step2: "2. 貼上 API Key，然後取得模型清單",
    step3: "3. 勾選需要的模型",
    presetDeepseek: "DeepSeek",
    presetDashscope: "通義千問（DashScope）",
    presetZhipu: "智譜 GLM",
    presetCustom: "自訂",
    urlPlaceholder: "https://api.example.com/v1",
    keyPlaceholder: "API Key（按下「新增」之前僅用於取得模型清單）",
    fetch: "取得模型清單",
    fetching: "取得中…",
    filter: "篩選模型",
    notFetched: "請點擊「取得模型清單」，或在下方手動輸入模型 ID。",
    alreadyAdded: "已新增",
    manualPlaceholder: "或手動輸入模型 ID",
    manualAdd: "使用此 ID",
    advanced: "進階：使用完整表單新增",
    addBtn: "新增 {n} 個模型",
    saving: "新增中…",
    added: "已新增 {n} 個模型",
    errUnauthorized: "服務商拒絕了該 API Key，請檢查後重試。",
    errNotFound: "該位址下沒有模型清單。請檢查 Base URL，或在下方手動輸入模型 ID。",
    errUnreachable: "無法連線到服務商，請檢查位址與網路。",
    errInvalidUrl: "請輸入以 http:// 或 https:// 開頭的完整位址",
    errEmpty: "服務商沒有回傳任何模型，請在下方手動輸入模型 ID。",
    errOther: "取得模型清單失敗。請檢查位址與 Key，或在下方手動輸入模型 ID。",
    errSave: "儲存失敗，請重試。",
  },
  title: "雲端模型",
  unpin: "取消置頂",
};

export default cloudModels;
