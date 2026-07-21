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
  title: "Cloud Models",
  unpin: "Unpin",
};

export default cloudModels;
