// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// i18n sub-file for Settings -> Extensions (ExtensionsPanel.vue). UTF-8.
// zh-CN / zh-TW mirror the en key structure exactly (tsc + parity test).

const extensions = {
  builtin: "内置",
  builtinHint: "随应用提供：可以停用，不能卸载。",
  title: "扩展",
  subtitle: "添加你自己的工具、技能、提示词和 MCP 服务器。扩展与应用同权限运行：只安装可信的扩展。",
  directory: "目录：",
  folderPlaceholder: "扩展文件夹路径（包含 extension.json）",
  empty: {
    title: "还没有安装扩展。",
    hint: "添加 .zip 或文件夹，或直接放进上面的目录后点「全部重新加载」。",
  },
  state: {
    loaded: "已启用",
    failed: "加载失败",
    disabled: "已停用",
    warnings: "有警告",
  },
  count: {
    tools: "{n} 个工具",
    skills: "{n} 个技能",
    mcp: "{n} 个 MCP 服务器",
    prompts: "提示词",
  },
  detail: {
    tools: "工具",
    skills: "技能",
    mcp: "MCP 服务器",
    prompts: "提示词",
    settings: "配置",
  },
  group: {
    read: "读取",
    edit: "编辑",
    command: "命令",
    none: "始终可用",
  },
  action: {
    addZip: "添加 .zip…",
    addFolder: "添加文件夹…",
    reloadAll: "全部重新加载",
    next: "下一步",
    details: "详情",
    less: "收起",
    settings: "配置",
    enable: "启用",
    disable: "停用",
    reload: "重新加载",
    working: "处理中…",
    uninstall: "卸载",
    install: "安装",
    update: "更新",
  },
  install: {
    runsFrontendCode: "该扩展还会在应用页面里运行 JavaScript，能使用你当前的登录会话。请只安装完全信任来源的扩展。",
    title: "安装扩展",
    titleUpdate: "更新扩展",
    replaces: "替换 v{v}",
    runsCode: "该扩展会在应用内运行 Python 代码。",
    trust: "目前什么都还没有运行。请只安装来源可信的扩展。",
  },
  settings: {
    title: "配置 — {name}",
    none: "该扩展没有可配置项。",
    secretKeep: "已保存 — 留空则保持不变",
    secretUnset: "未设置",
    keep: "保留",
    clear: "清除",
  },
  uninstall: {
    title: "卸载 {name}？",
    message: "它的工具、技能、提示词和 MCP 服务器会立即移除。",
    purge: "同时删除它的数据和已保存的密钥",
  },
  toast: {
    loadFailed: "无法加载扩展列表：{error}",
    failed: "{name} 加载失败：{error}",
    reloadedAll: "扩展已重新加载",
    reloaded: "{name} 已重新加载",
    enabled: "{name} 已启用",
    disabled: "{name} 已停用",
    uninstalled: "{name} 已卸载",
    installed: "{name} 已安装",
    settingsSaved: "{name} 的配置已保存",
  },
  mode: {
    frontendFailed: "扩展界面加载失败",
    close: "关闭",
    button: "扩展",
    exit: "退出该扩展模式",
    none: "还没有扩展在这里提供入口。",
    settings: "扩展设置…",
    run: "执行",
    actionFailed: "操作失败",
  },
};

export default extensions;
