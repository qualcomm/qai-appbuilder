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
  add: "Add Model",
  addFirst: "Add your first cloud model to get started",
  apiKey: "API Key",
  apiKeyKeepHint: "Saved key is not shown; leave blank to keep unchanged",
  apiKeyOptional: "API Key (optional)",
  baseUrlOptional: "Base URL (optional)",
  clone: "Clone",
  confirmDelete: "Are you sure you want to delete this cloud model?",
  confirmDeleteMsg: "Are you sure you want to delete \"{name}\" ({model_id})? This action cannot be undone.",
  contextLength: "Context Length (tokens)",
  delete: "Delete Model",
  deleteFailed: "Delete failed",
  deleted: "Cloud model deleted",
  description: "Description",
  displayNameRequired: "Display Name is required",
  edit: "Edit Model",
  endpoint: "Endpoint URL",
  hide: "Hide",
  isDefault: "Set as default",
  // Shown in place of the API key input for a keyless (auth_mode: "sso")
  // provider — the QAI Service token pool, authenticated by the signed-in
  // user's short-lived token rather than a key the user supplies.
  keylessProviderHint: "Signed-in account, no API Key needed",
  loadFailed: "Failed to load cloud models",
  modelAdded: "Model added",
  modelId: "Model ID",
  modelIdDesc: "Display id used in the model picker. Sent to the API as-is unless an API Model ID override is set.",
  apiModelId: "API Model ID (optional)",
  apiModelIdDesc: "Wire model name actually sent to the provider's API when it differs from the Model ID (e.g. dated \"claude-sonnet-4-20250514\"). Leave empty to use the Model ID.",
  modelIdDuplicate: "Model ID \"{model_id}\" already exists for this Provider",
  modelIdRequired: "Model ID is required",
  modelUpdated: "Model updated",
  name: "Display Name",
  noModels: "No cloud models configured",
  onboarding: {
    title: "No cloud model configured yet",
    desc: "Add a cloud model in Settings to chat with cloud providers (e.g. Claude, GPT). You can still use local on-device models without one.",
    cta: "Configure",
  },
  apiKeyOnboarding: {
    title: "Set your API Key",
    desc: "Cloud models are already pre-configured for you. Just enter your API Key to start using them.",
    cta: "Set API Key",
  },
  apiKeyDialog: {
    title: "Set API Key",
    subtitle: "This key applies to all pre-configured cloud models.",
    placeholder: "Enter your API Key",
    save: "Save",
    cancel: "Cancel",
    saveSuccess: "API Key saved",
    saveError: "Failed to save API Key",
  },
  apiKeyError: {
    // Friendly replacement for the raw provider_api_key_missing / 401 error.
    message: "This cloud model needs an API Key before you can use it.",
    setKeyCta: "Set API Key", // internal edition button
    goToSettingsCta: "Cloud Model Settings", // external edition button
  },
  pinProvider: "Pin this Provider",
  pinned: "Pinned",
  provider: "Provider",
  providerApiKey: "Provider API Key",
  providerApiKeyDesc: "Shared by all models under this Provider; takes priority over the global config.",
  providerBaseUrl: "Provider Base URL",
  providerBaseUrlDesc: "OpenAI API compatible endpoint, shared by all models under this Provider.",
  providerFieldSaved: "saved",
  providerRequired: "Provider is required.",
  save: "Save",
  saveFailed: "Save failed",
  saved: "Cloud model saved",
  search: "Search cloud models...",
  show: "Show",
  supportsStreaming: "Supports Streaming",
  paramsTitle: "Supported Parameters",
  paramsDesc:
    "Turn a parameter off when this model rejects it (the request would otherwise fail). The chat sampling panel still shows the control, but unsupported parameters are dropped before the request is sent.",
  paramThoughtSigHint:
    "Enable only for Vertex AI thinking models (e.g. Gemini) that require the thought signature to be echoed back.",
  gateway: {
    title: "Enterprise gateway (QAI Service)",
    desc: "Optional. Only for people who have a company QAI Service address. Models are provided by the gateway after you sign in with company SSO; nothing is enabled until you save an address and confirm.",
    urlPlaceholder: "https://qai-service.example.com:8012",
    urlInvalid: "Enter a full address starting with http:// or https://",
    emptyHint: "To turn it off, clear the address and press Disconnect.",
    saveBtn: "Save and connect",
    retryBtn: "Sign in and sync again",
    disconnectBtn: "Disconnect",
    stateOff: "Not connected",
    stateNeedLogin: "Address saved - sign-in needed",
    stateOn: "Connected - {n} models",
    phase: {
      saving: "Saving...",
      login: "Signing in with company SSO...",
      syncing: "Syncing models...",
    },
    confirmTitle: "Connect to QAI Service?",
    confirmMsg:
      "Address: {url}\n\nWhat will happen:\n1. The address is saved on this computer.\n2. You will be signed in with company SSO once more (a window may open).\n3. The model list is fetched from the gateway and appears in the model picker.\n\nIf anything fails you can press the button again.",
    confirmBtn: "Save and connect",
    disconnectTitle: "Disconnect QAI Service?",
    disconnectMsg:
      "The saved address, the gateway's models and the sign-in token for it will be removed from this computer. Company SSO login itself is not affected.",
    connected: "QAI Service connected ({n} models)",
    errSave: "Could not save the address. Please try again.",
    errLogin: "Sign-in was not completed (window closed or blocked). The address is saved; press the button to try again.",
    errNoToken: "Sign-in finished but the gateway did not issue a token. Check the address and your account, then try again.",
    errSyncFailed: "Could not fetch the model list from the gateway. Check the address and network, then try again.",
  },
  title: "Cloud Models",
  unpin: "Unpin",
};

export default cloudModels;
