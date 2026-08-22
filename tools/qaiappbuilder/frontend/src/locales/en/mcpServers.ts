// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。见 AGENTS.md §3.10。
// MCP (Model Context Protocol) servers settings panel.
// =============================================================================

const mcpServers = {
  title: "MCP Servers",
  subtitle:
    "Connect external Model Context Protocol servers to give the chat agent extra tools.",
  gateDisabled:
    "MCP is stopped. Turn on the switch above to enable it. While stopped, servers can be configured but their tools are not advertised to the model.",
  toolCount: "{count} tools",
  resourceCount: "{count} resources",
  promptCount: "{count} prompts",
  capabilities: {
    toolsTooltip: "Callable tools this MCP server exposes to the AI.",
    resourcesTooltip: "Readable resources this MCP server exposes to the AI.",
    promptsTooltip: "Prompt templates this MCP server exposes to the AI.",
  },
  lastTest: {
    testing: "Testing…",
    pass: "✓ Test passed",
    passWithTools: "✓ Test passed · {count} tools",
    fail: "✕ Test failed",
  },
  empty: {
    title: "No MCP servers configured",
    hint: "Add a server to expose its tools to the chat agent — e.g. a local stdio subprocess or a remote SSE/HTTP endpoint.",
  },
  status: {
    connected: "Connected",
    idle: "Not connected",
    disabledBadge: "Disabled",
  },
  global: {
    label: "MCP",
    on: "MCP on",
    off: "MCP off",
    enable: "Enable MCP",
    disable: "Disable MCP",
  },
  group: {
    added: "Added",
    addedHint: "Your configured MCP connections.",
    browse: "Browse marketplace",
    browseHint: "Add a new connection from built-in or online sources.",
  },
  field: {
    name: "Name",
    transport: "Transport",
    command: "Command",
    args: "Arguments",
    url: "URL",
    headers: "Headers",
    timeout: "Timeout (seconds)",
  },
  placeholder: {
    name: "my-server",
    command: "npx",
    args: "-y {'@'}modelcontextprotocol/server-filesystem /path",
    url: "https://example.com/mcp",
    headerKey: "Header name",
    headerValue: "Value (stored securely)",
  },
  modal: {
    title: "Add MCP server",
  },
  market: {
    title: "Browse marketplace",
    subtitle: "Browse MCP connections and add them with one click.",
    docs: "Docs",
    install: "Install",
    reinstall: "Reinstall",
    installing: "Installing…",
    installTitle: "Install {name}",
    installHint: "This server needs some values before it can start.",
    added: "Added",
    sourceAll: "All",
    sourceBadge: {
      curated: "Built-in",
      registry: "Online",
      builtin: "Built-in",
      builtinRegistry: "Anthropic Official MCP Source",
    },
    manageSources: "Manage sources",
    sourcesTitle: "Registry sources",
    refresh: "Refresh",
    refreshing: "Loading…",
    loadRegistry: "Load catalog",
    search: "Search by name…",
    loadMore: "Load more",
    loadingMore: "Loading…",
    registryEmpty: "No online servers loaded yet",
    registryEmptyHint:
      "Load installable servers from the online MCP catalog. Clicking will fetch the list over the network.",
    registryError:
      "Could not reach the online MCP catalog ({error}). Showing built-in servers only.",
    sourceOAuthPending: "Browser authentication page opened — please sign in to continue loading this registry source.",
    envFieldsHint: "This server needs credentials.",
    headerFieldsHint: "This server needs credentials.",
    headerLabels: {
      cebotXUserId: "Qualcomm User Name (email prefix, e.g. johndoe)",
    },
    credentialDeferredHint: "Note: your user name will be validated on the first tool call — please make sure it is correct.",
    oauthHint: "Click Authenticate to open the Microsoft Entra login page. After you sign in, the Bearer token will be filled in automatically.",
    oauthButton: "Authenticate",
    oauthPending: "Waiting for login…",
    addSource: "Add source",
    addSourceUrl: "Registry URL",
    addSourceName: "Display name (optional)",
    addSourceToken: "Bearer token (optional, for protected registries)",
    addSourceCookie: "Cookie (optional, for SSO/session authenticated registries)",
    addSourceHint: "Enter the URL of any MCP server registry. Supports the MCP Registry API (/v0/servers), Smithery (smithery.ai/servers), and other common formats. Example: https://registry.modelcontextprotocol.io",
    addSourceConfirm: "Add",
    addSourceCancel: "Cancel",
    configureSource: "Configure auth",
    configureSourceHint: "Enter an access token (Bearer) or session cookie for this built-in registry.",
    removeSource: "Remove source",
    removeSourceConfirm: "Remove \"{name}\" source?",
  },
  action: {
    add: "Add server",
    addHeader: "Add header",
    connect: "Connect",
    connecting: "Connecting…",
    test: "Test",
    testing: "Testing…",
    remove: "Remove",
    enable: "Enable",
    disable: "Disable",
  },
  confirm: {
    removeTitle: "Remove MCP server",
    removeMessage:
      "Remove \"{name}\" and drop all of its tools from the chat agent?",
  },
  toast: {
    added: "Connected \"{name}\" — {count} tools available",
    savedDisabled: "Saved \"{name}\" (MCP disabled — not connected)",
    savedConnecting: "Saved \"{name}\" — connecting in background",
    connectFailed: "Failed to connect \"{name}\": {error}",
    tested: "\"{name}\" reachable — {count} tools",
    removed: "Removed \"{name}\"",
    installed: "Installed \"{name}\" — {count} tools available",
    installedNoTools: "Installed \"{name}\" but no tools were found — please check your credentials",
    installedCredentialWarn: "Installed \"{name}\" but connection check failed ({error}) — please verify your credentials",
    credentialCheckFailed: "Credential check failed",
    enabled: "Enabled \"{name}\"",
    disabled: "Disabled \"{name}\"",
    globalEnabled: "MCP enabled",
    globalDisabled: "MCP disabled",
    globalFailed: "Failed to switch MCP: {error}",
    refreshed: "Loaded {count} online servers",
    refreshFailed: "Failed to refresh catalog: {error}",
    searchFailed: "Search failed: {error}",
    sourceAdded: "Registry source added",
    sourceRemoved: "Registry source removed",
    sourceCredentialsSaved: "Credentials saved",
  },
};

export default mcpServers;
