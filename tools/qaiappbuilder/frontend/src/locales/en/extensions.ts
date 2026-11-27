// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// i18n sub-file for Settings -> Extensions (ExtensionsPanel.vue). UTF-8.
// zh-CN / zh-TW mirror the en key structure exactly (tsc + parity test).

const extensions = {
  builtin: "Built-in",
  builtinHint: "Ships with the app: you can disable it, not uninstall it.",
  title: "Extensions",
  subtitle: "Add your own tools, skills, prompts and MCP servers. Extensions run with the same rights as the app: install only ones you trust.",
  directory: "Folder:",
  folderPlaceholder: "Path to an extension folder (contains extension.json)",
  empty: {
    title: "No extensions installed.",
    hint: "Add a .zip or a folder, or put one into the folder above and click Reload all.",
  },
  state: {
    loaded: "Enabled",
    failed: "Failed",
    disabled: "Disabled",
    warnings: "Has warnings",
  },
  count: {
    tools: "{n} tools",
    skills: "{n} skills",
    mcp: "{n} MCP servers",
    prompts: "Prompts",
  },
  detail: {
    tools: "Tools",
    skills: "Skills",
    mcp: "MCP servers",
    prompts: "Prompts",
    settings: "Settings",
  },
  group: {
    read: "read",
    edit: "edit",
    command: "command",
    none: "always on",
  },
  action: {
    addZip: "Add .zip…",
    addFolder: "Add folder…",
    reloadAll: "Reload all",
    next: "Next",
    details: "Details",
    less: "Less",
    settings: "Settings",
    enable: "Enable",
    disable: "Disable",
    reload: "Reload",
    working: "Working…",
    uninstall: "Uninstall",
    install: "Install",
    update: "Update",
  },
  install: {
    runsFrontendCode: "This extension also runs JavaScript inside the app's own page, with your session. Only install it if you fully trust where it came from.",
    title: "Install extension",
    titleUpdate: "Update extension",
    replaces: "replaces v{v}",
    runsCode: "This extension runs Python code inside the app.",
    trust: "Nothing has run yet. Install it only if you trust where it came from.",
  },
  settings: {
    title: "Settings — {name}",
    none: "This extension has no settings.",
    secretKeep: "Stored — leave empty to keep",
    secretUnset: "Not set",
    keep: "Keep",
    clear: "Clear",
  },
  uninstall: {
    title: "Uninstall {name}?",
    message: "Its tools, skills, prompts and MCP servers are removed at once.",
    purge: "Also delete its data and saved secrets",
  },
  toast: {
    loadFailed: "Could not load extensions: {error}",
    failed: "{name} failed to load: {error}",
    reloadedAll: "Extensions reloaded",
    reloaded: "{name} reloaded",
    enabled: "{name} enabled",
    disabled: "{name} disabled",
    uninstalled: "{name} uninstalled",
    installed: "{name} installed",
    settingsSaved: "Settings of {name} saved",
  },
  mode: {
    frontendFailed: "Extension UI failed to load",
    close: "Close",
    button: "Extension",
    exit: "Leave this extension mode",
    none: "No extension adds an entry here yet.",
    settings: "Extension settings…",
    run: "Run",
    actionFailed: "The action failed",
  },
};

export default extensions;
