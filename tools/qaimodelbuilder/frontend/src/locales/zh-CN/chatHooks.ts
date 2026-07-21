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

const chatHooks = {
  title: "Hook 管理",
  subtitle: "AI 在该事件点自动执行的 shell 命令",
  empty: "尚未配置任何 Hook。点击\"添加 Hook\"创建一个。",
  loadFailed: "加载 Hook 失败",
  saveFailed: "保存 Hook 失败",
  saved: "Hook 已保存",
  field: {
    event: "事件",
    command: "命令",
    timeout: "超时（秒）",
  },
  placeholder: {
    command: "例如：ruff check .",
  },
  action: {
    add: "添加 Hook",
    delete: "删除",
    save: "保存",
    saving: "保存中…",
  },
  confirm: {
    deleteTitle: "删除 Hook",
    deleteMessage: "确定删除此 Hook 吗？此操作无法撤销。",
    deleteConfirm: "删除",
    cancel: "取消",
  },
  enable: {
    label: "启用 Hook",
    securityWarning:
      "启用后，你配置的 shell 命令将被自动执行（任意命令执行）。只有在你完全信任每一条 Hook 命令时才启用。",
    disabledHint:
      "Hook 已禁用。你仍可编辑下方配置，但在启用之前不会执行任何命令。",
    loadFailed: "加载 Hook 启用状态失败",
    saveFailed: "更新 Hook 启用状态失败",
    savedOn: "已启用 Hook",
    savedOff: "已禁用 Hook",
  },
  docs: {
    title: "通过 pre_tool_call Hook 干预工具调用",
    intro:
      "pre_tool_call Hook 可通过在 stdout 打印 JSON（退出码 0）来干预工具调用。可识别的键：",
    deny: "阻止调用；模型会收到 \"[hook_blocked] {reason}\"。",
    allow: "继续执行（默认行为）。",
    updatedInput: "在工具运行前替换其参数。",
    additionalContext: "（用于 pre_message / on_user_input Hook）将额外文本并入本轮对话。",
    observer: "输出普通文本 / 非 JSON 时，Hook 仅作为观察者（行为不变）。",
    exampleLabel: "示例：",
  },
  subagents: {
    title: "子智能体模型",
    subtitle:
      "为每个子智能体档位选择使用的模型。保持「继承」则使用主对话模型。",
    inherit: "（继承主模型）",
    loadFailed: "加载子智能体模型失败",
    saveFailed: "保存子智能体模型失败",
    saved: "子智能体模型已保存",
    profile: {
      explore: {
        label: "探索",
        desc: "只读的搜索专家，用于代码库探索。",
      },
      general: {
        label: "通用",
        desc: "具备全部工具的子智能体，用于通用任务。",
      },
    },
  },
  event: {
    pre_tool_call: "工具调用前",
    post_tool_call: "工具调用后",
    pre_message: "消息发送前",
    post_message: "消息发送后",
    on_error: "出错时",
    on_complete: "完成时",
    on_user_input: "用户输入时",
    on_session_start: "会话开始时",
    on_session_end: "会话结束时",
    on_truncate: "截断时",
  },
};

export default chatHooks;
