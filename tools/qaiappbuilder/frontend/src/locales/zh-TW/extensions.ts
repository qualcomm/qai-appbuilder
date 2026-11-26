// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// i18n sub-file for Settings -> Extensions (ExtensionsPanel.vue). UTF-8.
// zh-CN / zh-TW mirror the en key structure exactly (tsc + parity test).

const extensions = {
  title: "擴充",
  subtitle: "加入你自己的工具、技能、提示詞和 MCP 伺服器。擴充與應用程式同權限執行：只安裝可信的擴充。",
  directory: "目錄：",
  folderPlaceholder: "擴充資料夾路徑（包含 extension.json）",
  empty: {
    title: "尚未安裝擴充。",
    hint: "加入 .zip 或資料夾，或直接放進上方目錄後按「全部重新載入」。",
  },
  state: {
    loaded: "已啟用",
    failed: "載入失敗",
    disabled: "已停用",
    warnings: "有警告",
  },
  count: {
    tools: "{n} 個工具",
    skills: "{n} 個技能",
    mcp: "{n} 個 MCP 伺服器",
    prompts: "提示詞",
  },
  detail: {
    tools: "工具",
    skills: "技能",
    mcp: "MCP 伺服器",
    prompts: "提示詞",
    settings: "設定",
  },
  group: {
    read: "讀取",
    edit: "編輯",
    command: "命令",
    none: "一律可用",
  },
  action: {
    addZip: "加入 .zip…",
    addFolder: "加入資料夾…",
    reloadAll: "全部重新載入",
    next: "下一步",
    details: "詳細",
    less: "收合",
    settings: "設定",
    enable: "啟用",
    disable: "停用",
    reload: "重新載入",
    working: "處理中…",
    uninstall: "解除安裝",
    install: "安裝",
    update: "更新",
  },
  install: {
    title: "安裝擴充",
    titleUpdate: "更新擴充",
    replaces: "取代 v{v}",
    runsCode: "此擴充會在應用程式內執行 Python 程式碼。",
    trust: "目前尚未執行任何東西。請只安裝來源可信的擴充。",
  },
  settings: {
    title: "設定 — {name}",
    none: "此擴充沒有可設定項目。",
    secretKeep: "已儲存 — 留空則維持不變",
    secretUnset: "未設定",
    keep: "保留",
    clear: "清除",
  },
  uninstall: {
    title: "解除安裝 {name}？",
    message: "它的工具、技能、提示詞和 MCP 伺服器會立即移除。",
    purge: "同時刪除它的資料和已儲存的密鑰",
  },
  toast: {
    loadFailed: "無法載入擴充清單：{error}",
    failed: "{name} 載入失敗：{error}",
    reloadedAll: "擴充已重新載入",
    reloaded: "{name} 已重新載入",
    enabled: "{name} 已啟用",
    disabled: "{name} 已停用",
    uninstalled: "{name} 已解除安裝",
    installed: "{name} 已安裝",
    settingsSaved: "{name} 的設定已儲存",
  },
};

export default extensions;
