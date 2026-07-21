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
  title: "雲端模型",
  unpin: "取消置頂",
};

export default cloudModels;
