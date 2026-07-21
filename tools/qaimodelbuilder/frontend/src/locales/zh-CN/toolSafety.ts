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
// toolSafety — 工具防护 / Tool Safety 面板（2026-06 安全设置统一治理）。
// =============================================================================

const toolSafety = {
  title: "工具防护",
  subtitle: "为大模型工具调用提供两层防护。",
  // ── 第 1 层 — 纯软件工具防护（热生效）──
  layer1Title: "工具防护（始终可用）",
  layer1Desc:
    "对每次工具调用施加的纯软件准入防护。改动立即生效。",
  fileBrokerEnabled: "启用 File Broker",
  fileBrokerDesc:
    "敏感路径排除、危险写入/执行拦截，以及 glob/grep 结果截断。",
  fileBrokerRebootHint: "切换 File Broker 需要重启以重建工具桥。",
  maxEntries: "最大条目数（glob/grep）",
  projectSkipDirs: "项目跳过目录",
  projectSkipDirsDesc: "在项目级工具扫描中排除的目录名。",
  projectSkipDirsPlaceholder: "node_modules",
  globalProxy: "全局代理",
  globalProxyDesc: "网络工具使用的 HTTP(S) 代理地址。留空表示不使用。",
  globalProxyPlaceholder: "http://proxy.example:8080",
  // ── 第 2 层 — PolicyCenter FileGuard ──
  layer2Title: "策略守卫（FileGuard）",
  layer2Desc:
    "由 PolicyCenter 依据当前策略对读 / 写 / 执行权限进行强制校验。改动需要重启。",
  fileGuardEnabled: "启用 FileGuard",
  fileGuardDesc: "对工具的读 / 写 / 执行权限进行强制校验，并同时守护子进程发起的文件访问。",
  allowExecTool: "允许 exec 工具",
  allowExecToolDesc: "关闭后，exec 工具会在任何 broker 校验前被直接拒绝。",
  // ── 始终开启的安全底线（3c 开关树 §6.4）——不可关的基线防护 ──
  alwaysOn: {
    title: "始终开启的底线",
    desc: "无法关闭的基线防护。它们不读取安全总闸，即使处于宽松（permissive）或关闭（disabled）模式下也始终强制生效。",
    banner: "这些底线始终开启，无法从界面上关闭。",
    badge: "始终开启",
    lockedTitle: "该防护始终开启，无法关闭。",
    protectedPathsLabel: "受保护的系统路径",
    protectedPathsDesc: "对关键系统位置的写入始终被拦截，不受任何策略或授权影响。",
    dangerousBuiltinsLabel: "危险命令底线",
    dangerousBuiltinsDesc: "内置的破坏性命令清单始终需要显式批准，永远不会被静默放行。",
    mainProcessHookLabel: "主进程审计钩子",
    mainProcessHookDesc: "主进程及其子进程始终被审计；审计哨兵不读取总闸开关。",
  },
  // ── 第 3 层（OS 隔离沙箱）已于 2026-07-01 与 Persistent ACL 一起移除 ──
  // ── 自定义危险命令模式（P-10，仅增不删的追加层）──
  dangerousCommands: {
    title: "自定义危险命令模式",
    desc: "在始终生效的内置底线之上，追加拦截破坏性命令的自定义正则表达式。自定义模式只能新增拦截范围，绝不会移除内置防护。",
    builtinBanner: "内置底线模式始终强制生效，不可移除。",
    builtinLabel: "内置底线（只读）",
    builtinLockedTitle: "该内置模式始终强制生效，不可移除。",
    extraLabel: "自定义模式",
    extraDesc: "命令执行前会用这些正则表达式（不区分大小写）进行匹配。",
    extraPlaceholder: "例如 \\bshutdown\\b",
    rebootHint: "自定义模式在启动时应用，保存后需重启才能生效。",
    save: "保存模式",
    invalidPatterns: "以下模式不是合法的正则表达式，已被丢弃：{patterns}",
  },
  // ── 工具输出上限（构建期 → 需重启）──
  outputLimitsTitle: "工具输出上限",
  outputLimitsDesc:
    "限制每个工具返回给模型的结果体量。值越大，模型获得的上下文越多，但消耗的 token 也越多。被截断的内容会落盘，可用 read 工具取回。改动其中任意一项都需要重启才能生效。",
  readMaxLines: "read — 最大行数",
  readMaxLinesDesc:
    "read 工具单次返回的最大行数；超出部分会被截断。",
  readMaxBytes: "read — 最大字节数",
  readMaxBytesDesc:
    "read 工具单次返回的最大字节数；更大的文件会被截断。",
  readMaxLineLength: "read — 单行最大长度",
  readMaxLineLengthDesc:
    "read 返回时，超过该字符数的行会被截断。",
  globMaxResults: "glob — 最大结果数",
  globMaxResultsDesc:
    "glob 工具一次最多给模型展示多少个文件路径；超出部分会落盘，可用 read 工具取回。",
  grepMaxMatches: "grep — 最大匹配数",
  grepMaxMatchesDesc:
    "grep 工具一次最多给模型展示多少条匹配；超出部分会落盘，可用 read 工具取回。",
  grepMaxLineLength: "grep — 单行最大长度",
  grepMaxLineLengthDesc:
    "grep 返回匹配时，超过该字符数的行会被截断。",
  grepMaxOutputBytes: "grep — 最大输出字节数",
  grepMaxOutputBytesDesc:
    "grep 单次交给模型的输出总字节上限；超出部分会被截断。",
  // ── 重启确认对话框（决策 3B）──
  rebootTitle: "需要重启",
  rebootMessage:
    "已修改并保存安全相关配置，但需要重启才能生效。是否立即重启？",
  rebootConfirm: "立即重启",
  rebootCancel: "稍后",
  rebootDeferred: "已保存。该改动将在下次重启后生效。",
  // ── 状态 ──
  saved: "已保存",
  saveFailed: "保存失败",
  add: "添加",
  remove: "移除",
};

export default toolSafety;
