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
  subtitle: "AI 在該事件點自動執行的 shell 命令",
  empty: "尚未設定任何 Hook。點選\"新增 Hook\"建立一個。",
  loadFailed: "載入 Hook 失敗",
  saveFailed: "儲存 Hook 失敗",
  saved: "Hook 已儲存",
  field: {
    event: "事件",
    command: "命令",
    timeout: "逾時（秒）",
  },
  placeholder: {
    command: "例如：ruff check .",
  },
  action: {
    add: "新增 Hook",
    delete: "刪除",
    save: "儲存",
    saving: "儲存中…",
  },
  confirm: {
    deleteTitle: "刪除 Hook",
    deleteMessage: "確定刪除此 Hook 嗎？此操作無法復原。",
    deleteConfirm: "刪除",
    cancel: "取消",
  },
  enable: {
    label: "啟用 Hook",
    securityWarning:
      "啟用後，你設定的 shell 命令將被自動執行（任意命令執行）。只有在你完全信任每一條 Hook 命令時才啟用。",
    disabledHint:
      "Hook 已停用。你仍可編輯下方設定，但在啟用之前不會執行任何命令。",
    loadFailed: "載入 Hook 啟用狀態失敗",
    saveFailed: "更新 Hook 啟用狀態失敗",
    savedOn: "已啟用 Hook",
    savedOff: "已停用 Hook",
  },
  docs: {
    title: "透過 pre_tool_call Hook 介入工具呼叫",
    intro:
      "pre_tool_call Hook 可透過在 stdout 印出 JSON（結束碼 0）來介入工具呼叫。可識別的鍵：",
    deny: "封鎖呼叫；模型會收到 \"[hook_blocked] {reason}\"。",
    allow: "繼續執行（預設行為）。",
    updatedInput: "在工具執行前替換其參數。",
    additionalContext: "（用於 pre_message / on_user_input Hook）將額外文字併入本輪對話。",
    observer: "輸出純文字 / 非 JSON 時，Hook 僅作為觀察者（行為不變）。",
    exampleLabel: "範例：",
  },
  subagents: {
    title: "子代理模型",
    subtitle:
      "為每個子代理設定檔選擇使用的模型。保持「繼承」則使用主對話模型。",
    inherit: "（繼承主模型）",
    loadFailed: "載入子代理模型失敗",
    saveFailed: "儲存子代理模型失敗",
    saved: "子代理模型已儲存",
    profile: {
      explore: {
        label: "探索",
        desc: "唯讀的搜尋專家，用於程式碼庫探索。",
      },
      general: {
        label: "通用",
        desc: "具備全部工具的子代理，用於通用任務。",
      },
    },
  },
  event: {
    pre_tool_call: "工具呼叫前",
    post_tool_call: "工具呼叫後",
    pre_message: "訊息傳送前",
    post_message: "訊息傳送後",
    on_error: "出錯時",
    on_complete: "完成時",
    on_user_input: "使用者輸入時",
    on_session_start: "工作階段開始時",
    on_session_end: "工作階段結束時",
    on_truncate: "截斷時",
  },
};

export default chatHooks;
