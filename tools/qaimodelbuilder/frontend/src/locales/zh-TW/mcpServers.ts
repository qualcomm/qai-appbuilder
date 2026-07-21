// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工維護，UTF-8（無 BOM）。見 AGENTS.md §3.10。
// MCP (Model Context Protocol) 伺服器設定面板。
// =============================================================================

const mcpServers = {
  title: "MCP 伺服器",
  subtitle:
    "連接外部 Model Context Protocol 伺服器，為聊天智慧代理提供額外工具。",
  gateDisabled:
    "MCP 已停用，打開上方開關即可啟用。停用狀態下可以設定伺服器，但其工具不會提供給模型。",
  toolCount: "{count} 個工具",
  resourceCount: "{count} 個資源",
  promptCount: "{count} 個提示詞",
  capabilities: {
    toolsTooltip: "該 MCP 伺服器向 AI 提供的可呼叫工具數量。",
    resourcesTooltip: "該 MCP 伺服器向 AI 提供的可讀取資源數量。",
    promptsTooltip: "該 MCP 伺服器向 AI 提供的提示詞範本數量。",
  },
  lastTest: {
    testing: "測試中…",
    pass: "✓ 測試通過",
    passWithTools: "✓ 測試通過 · {count} 個工具",
    fail: "✕ 測試失敗",
  },
  empty: {
    title: "尚未設定 MCP 伺服器",
    hint: "新增一個伺服器即可將其工具提供給聊天智慧代理 —— 例如本機 stdio 子行程或遠端 SSE/HTTP 端點。",
  },
  status: {
    connected: "已連接",
    idle: "未連接",
    disabledBadge: "已停用",
  },
  global: {
    label: "MCP",
    on: "MCP 已啟用",
    off: "MCP 已停用",
    enable: "啟用 MCP",
    disable: "停用 MCP",
  },
  group: {
    added: "已新增",
    addedHint: "你已設定的 MCP 連線。",
    browse: "瀏覽市集",
    browseHint: "從內建或線上來源新增連線。",
  },
  field: {
    name: "名稱",
    transport: "傳輸方式",
    command: "命令",
    args: "參數",
    url: "URL",
    headers: "請求標頭",
    timeout: "逾時（秒）",
  },
  placeholder: {
    name: "my-server",
    command: "npx",
    args: "-y {'@'}modelcontextprotocol/server-filesystem /path",
    url: "https://example.com/mcp",
    headerKey: "標頭名稱",
    headerValue: "值（安全儲存）",
  },
  modal: {
    title: "新增 MCP 伺服器",
  },
  market: {
    title: "瀏覽市集",
    subtitle: "瀏覽 MCP 連線，一鍵新增。",
    docs: "文件",
    install: "安裝",
    reinstall: "重新安裝",
    installing: "安裝中…",
    installTitle: "安裝 {name}",
    installHint: "該伺服器在啟動前需要填寫一些值。",
    added: "已新增",
    sourceAll: "全部",
    sourceBadge: {
      curated: "內建推薦",
      registry: "線上取得",
    },
    refresh: "重新整理",
    refreshing: "載入中…",
    loadRegistry: "載入目錄",
    search: "依名稱搜尋…",
    loadMore: "載入更多",
    loadingMore: "載入中…",
    registryEmpty: "尚未載入線上伺服器",
    registryEmptyHint:
      "從線上 MCP 目錄載入可安裝的伺服器。點擊將連網擷取清單。",
    registryError: "無法連接線上 MCP 目錄（{error}），僅顯示內建伺服器。",
    envFieldsHint: "該伺服器需要憑證。",
    headerFieldsHint: "該伺服器需要憑證。",
  },
  action: {
    add: "新增伺服器",
    addHeader: "新增標頭",
    connect: "連接",
    connecting: "連接中…",
    test: "測試",
    testing: "測試中…",
    remove: "移除",
    enable: "啟用",
    disable: "停用",
  },
  confirm: {
    removeTitle: "移除 MCP 伺服器",
    removeMessage: "移除「{name}」並從聊天智慧代理中刪除其全部工具？",
  },
  toast: {
    added: "已連接「{name}」 —— 可用 {count} 個工具",
    savedDisabled: "已儲存「{name}」（MCP 已停用 —— 未連接）",
    connectFailed: "連接「{name}」失敗：{error}",
    tested: "「{name}」可達 —— {count} 個工具",
    removed: "已移除「{name}」",
    installed: "已安裝「{name}」 —— 可用 {count} 個工具",
    enabled: "已啟用「{name}」",
    disabled: "已停用「{name}」",
    globalEnabled: "MCP 已啟用",
    globalDisabled: "MCP 已停用",
    globalFailed: "切換 MCP 失敗：{error}",
    refreshed: "已載入 {count} 個線上伺服器",
    refreshFailed: "重新整理目錄失敗：{error}",
    searchFailed: "搜尋失敗：{error}",
  },
};

export default mcpServers;
