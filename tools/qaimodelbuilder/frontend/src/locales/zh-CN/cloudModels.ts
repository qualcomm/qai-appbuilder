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
  add: "添加模型",
  addFirst: "添加第一个云端模型开始使用",
  apiKey: "API 密钥",
  apiKeyKeepHint: "已保存的密钥不回显，留空保持不变",
  apiKeyOptional: "API Key（可选）",
  baseUrlOptional: "Base URL（可选）",
  clone: "克隆",
  confirmDelete: "确定要删除此云端模型吗？",
  confirmDeleteMsg: "确定要删除 \"{name}\"（{model_id}）吗？此操作无法撤销。",
  contextLength: "上下文长度 (tokens)",
  delete: "删除模型",
  deleteFailed: "删除失败",
  deleted: "云端模型已删除",
  description: "描述",
  displayNameRequired: "Display Name 不能为空",
  edit: "编辑模型",
  endpoint: "接口地址",
  hide: "隐藏",
  isDefault: "设为默认",
  loadFailed: "加载云端模型失败",
  modelAdded: "模型已添加",
  modelId: "模型 ID",
  modelIdDesc: "在模型选择器中显示的 id。未设置「API 模型 ID」覆盖时原样发送给 API。",
  apiModelId: "API 模型 ID（可选）",
  apiModelIdDesc: "当与「模型 ID」不同时，实际发送给 Provider API 的上游模型名（如带日期的 \"claude-sonnet-4-20250514\"）。留空则使用「模型 ID」。",
  modelIdDuplicate: "该 Provider 下 Model ID \"{model_id}\" 已存在",
  modelIdRequired: "Model ID 不能为空",
  modelUpdated: "模型已更新",
  name: "显示名称",
  noModels: "暂无云端模型配置",
  onboarding: {
    title: "尚未配置云端模型",
    desc: "在设置中添加云端模型即可使用云端服务商（如 Claude、GPT）对话。未配置时仍可使用本地设备模型。",
    cta: "去配置",
  },
  apiKeyOnboarding: {
    title: "设置你的 API 密钥",
    desc: "云端模型已为你预先配置好，只需填入 API 密钥即可开始使用。",
    cta: "设置 API 密钥",
  },
  apiKeyDialog: {
    title: "设置 API 密钥",
    subtitle: "该密钥将应用于所有预配置的云端模型。",
    placeholder: "请输入你的 API 密钥",
    save: "保存",
    cancel: "取消",
    saveSuccess: "API 密钥已保存",
    saveError: "保存 API 密钥失败",
  },
  apiKeyError: {
    message: "该云端模型需要先设置 API 密钥才能使用。",
    setKeyCta: "设置 API 密钥",
    goToSettingsCta: "云端模型设置",
  },
  pinProvider: "置顶此 Provider",
  pinned: "已置顶",
  provider: "提供商",
  providerApiKey: "提供商 API 密钥",
  providerApiKeyDesc: "此 Provider 下所有模型共用，优先级高于全局配置。",
  providerBaseUrl: "提供商接入地址",
  providerBaseUrlDesc: "OpenAI API 兼容地址，此 Provider 下所有模型共用。",
  providerFieldSaved: "已保存",
  providerRequired: "Provider 不能为空",
  save: "保存",
  saveFailed: "保存失败",
  saved: "云端模型已保存",
  search: "搜索云端模型...",
  show: "显示",
  supportsStreaming: "支持流式输出",
  paramsTitle: "支持的参数",
  paramsDesc:
    "若该模型不支持某个参数（发送会导致请求失败），请将其关闭。聊天采样面板仍会显示对应控件，但不支持的参数会在请求发送前被自动剔除。",
  paramThoughtSigHint:
    "仅在需要回传思考签名的 Vertex AI 思考模型（如 Gemini）上开启。",
  title: "云端模型",
  unpin: "取消置顶",
};

export default cloudModels;
