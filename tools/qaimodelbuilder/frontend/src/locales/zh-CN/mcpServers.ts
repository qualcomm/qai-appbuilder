// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。见 AGENTS.md §3.10。
// MCP (Model Context Protocol) 服务器设置面板。
// =============================================================================

const mcpServers = {
  title: "MCP 服务器",
  subtitle:
    "连接外部 Model Context Protocol 服务器，为聊天智能体提供额外工具。",
  gateDisabled:
    "MCP 已停用，打开上方开关即可启用。停用状态下可以配置服务器，但其工具不会提供给模型。",
  toolCount: "{count} 个工具",
  resourceCount: "{count} 个资源",
  promptCount: "{count} 个提示词",
  capabilities: {
    toolsTooltip: "该 MCP 服务器向 AI 提供的可调用工具数量。",
    resourcesTooltip: "该 MCP 服务器向 AI 提供的可读取资源数量。",
    promptsTooltip: "该 MCP 服务器向 AI 提供的提示词模板数量。",
  },
  lastTest: {
    testing: "测试中…",
    pass: "✓ 测试通过",
    passWithTools: "✓ 测试通过 · {count} 个工具",
    fail: "✕ 测试失败",
  },
  empty: {
    title: "尚未配置 MCP 服务器",
    hint: "添加一个服务器即可将其工具暴露给聊天智能体 —— 例如本地 stdio 子进程或远程 SSE/HTTP 端点。",
  },
  status: {
    connected: "已连接",
    idle: "未连接",
    disabledBadge: "已禁用",
  },
  global: {
    label: "MCP",
    on: "MCP 已启用",
    off: "MCP 已停用",
    enable: "启用 MCP",
    disable: "停用 MCP",
  },
  group: {
    added: "已添加",
    addedHint: "你已配置的 MCP 连接。",
    browse: "浏览市场",
    browseHint: "从内置或在线来源添加新的连接。",
  },
  field: {
    name: "名称",
    transport: "传输方式",
    command: "命令",
    args: "参数",
    url: "URL",
    headers: "请求头",
    timeout: "超时（秒）",
  },
  placeholder: {
    name: "my-server",
    command: "npx",
    args: "-y {'@'}modelcontextprotocol/server-filesystem /path",
    url: "https://example.com/mcp",
    headerKey: "请求头名称",
    headerValue: "值（安全存储）",
  },
  modal: {
    title: "添加 MCP 服务器",
  },
  market: {
    title: "浏览市场",
    subtitle: "浏览 MCP 连接，一键添加。",
    docs: "文档",
    install: "安装",
    reinstall: "重新安装",
    installing: "安装中…",
    installTitle: "安装 {name}",
    installHint: "该服务器在启动前需要填写一些值。",
    added: "已添加",
    sourceAll: "全部",
    sourceBadge: {
      curated: "内置推荐",
      registry: "在线获取",
    },
    refresh: "刷新",
    refreshing: "加载中…",
    loadRegistry: "加载目录",
    search: "按名称搜索…",
    loadMore: "加载更多",
    loadingMore: "加载中…",
    registryEmpty: "尚未加载在线服务器",
    registryEmptyHint:
      "从在线 MCP 目录加载可安装的服务器。点击将联网拉取列表。",
    registryError: "无法连接在线 MCP 目录（{error}），仅显示内置服务器。",
    envFieldsHint: "该服务器需要凭据。",
    headerFieldsHint: "该服务器需要凭据。",
  },
  action: {
    add: "添加服务器",
    addHeader: "添加请求头",
    connect: "连接",
    connecting: "连接中…",
    test: "测试",
    testing: "测试中…",
    remove: "移除",
    enable: "启用",
    disable: "禁用",
  },
  confirm: {
    removeTitle: "移除 MCP 服务器",
    removeMessage: "移除“{name}”并从聊天智能体中删除其全部工具？",
  },
  toast: {
    added: "已连接“{name}” —— 可用 {count} 个工具",
    savedDisabled: "已保存“{name}”（MCP 已禁用 —— 未连接）",
    connectFailed: "连接“{name}”失败：{error}",
    tested: "“{name}”可达 —— {count} 个工具",
    removed: "已移除“{name}”",
    installed: "已安装“{name}” —— 可用 {count} 个工具",
    enabled: "已启用“{name}”",
    disabled: "已禁用“{name}”",
    globalEnabled: "MCP 已启用",
    globalDisabled: "MCP 已停用",
    globalFailed: "切换 MCP 失败：{error}",
    refreshed: "已加载 {count} 个在线服务器",
    refreshFailed: "刷新目录失败：{error}",
    searchFailed: "搜索失败：{error}",
  },
};

export default mcpServers;
