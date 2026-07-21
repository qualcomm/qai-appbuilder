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
// 承载 /api/security/runtime-config 的两层安全开关：
//   层 1 工具防护（tools.*，热生效）/ 层 2 策略守卫（FileGuard，需重启）。
// （Layer 3 OS 隔离沙箱 / Windows AppContainer 已于 2026-07-01 与 Persistent ACL
//  一起清理；运行时 DTO 上的 sandbox_enabled 字段保留但无 UI 入口。）
// =============================================================================

const toolSafety = {
  title: "Tool Safety",
  subtitle: "Two layers of protection for LLM tool calls.",
  // ── Layer 1 — pure-software tool guard (hot-applies) ──
  layer1Title: "Tool Guard (always available)",
  layer1Desc:
    "Pure-software hygiene applied to every tool call. Changes take effect immediately.",
  fileBrokerEnabled: "Enable File Broker",
  fileBrokerDesc:
    "Sensitive-path exclusion, dangerous write/exec interception and glob/grep result truncation.",
  fileBrokerRebootHint: "Toggling the File Broker requires a restart to rebuild the tool bridge.",
  maxEntries: "Max Entries (glob/grep)",
  projectSkipDirs: "Project skip directories",
  projectSkipDirsDesc: "Directory names excluded from project-wide tool scans.",
  projectSkipDirsPlaceholder: "node_modules",
  globalProxy: "Global proxy",
  globalProxyDesc: "HTTP(S) proxy URL used by network tools. Leave empty for none.",
  globalProxyPlaceholder: "http://proxy.example:8080",
  // ── Layer 2 — PolicyCenter FileGuard ──
  layer2Title: "Policy Guard (FileGuard)",
  layer2Desc:
    "PolicyCenter enforcement of read / write / exec permissions per the active policy. Changing it requires a restart.",
  fileGuardEnabled: "Enable FileGuard",
  fileGuardDesc:
    "Enforces read / write / exec permissions for tools and also guards file access made by spawned subprocesses.",
  allowExecTool: "Allow exec tool",
  allowExecToolDesc: "When off, the exec tool is hard-denied before any broker check.",
  // ── Always-on security floors (3c switch-tree §6.4) — immutable baseline ──
  alwaysOn: {
    title: "Always-on baseline",
    desc: "Baseline protections that cannot be turned off. They ignore the security master switch and stay enforced even in permissive or disabled mode.",
    banner: "These floors are always on and cannot be disabled from the UI.",
    badge: "Always-on",
    lockedTitle: "This protection is always on and cannot be turned off.",
    protectedPathsLabel: "Protected system paths",
    protectedPathsDesc: "Writes to critical system locations are always blocked regardless of any policy or grant.",
    dangerousBuiltinsLabel: "Dangerous command floor",
    dangerousBuiltinsDesc: "The built-in list of destructive commands always requires explicit approval and can never be silently allowed.",
    mainProcessHookLabel: "Main-process audit hook",
    mainProcessHookDesc: "The main process and its children are always audited; the audit sentinels never read the master switch.",
  },
  // ── Layer 3 removed 2026-07-01 (Windows ACL / sandbox cleanup) ──
  // ── Custom dangerous-command patterns (P-10) — union-only override ──
  dangerousCommands: {
    title: "Custom dangerous-command patterns",
    desc: "Add extra regular expressions that block destructive commands, on top of the always-on built-in floor. Custom patterns can only ADD coverage — they never remove the built-in protections.",
    builtinBanner: "Built-in floor patterns are always enforced and cannot be removed.",
    builtinLabel: "Built-in floor (read-only)",
    builtinLockedTitle: "This built-in pattern is always enforced and cannot be removed.",
    extraLabel: "Custom patterns",
    extraDesc: "Extra regular expressions matched (case-insensitive) against commands before they run.",
    extraPlaceholder: "e.g. \\bshutdown\\b",
    rebootHint: "Custom patterns are applied at startup, so saving requires a restart to take effect.",
    save: "Save patterns",
    invalidPatterns: "These patterns are not valid regular expressions and were dropped: {patterns}",
  },
  // ── Tool output limits (build-time → reboot) ──
  outputLimitsTitle: "Tool Output Limits",
  outputLimitsDesc:
    "Caps on how much each tool hands back to the model. Larger values give the model more context but cost more tokens. Truncated content is written to disk and can be retrieved with the read tool. Changing any limit requires a restart.",
  readMaxLines: "read — max lines",
  readMaxLinesDesc:
    "Maximum number of lines the read tool returns in one call; beyond this the output is truncated.",
  readMaxBytes: "read — max bytes",
  readMaxBytesDesc:
    "Maximum byte size the read tool returns in one call; larger files are truncated.",
  readMaxLineLength: "read — max line length",
  readMaxLineLengthDesc:
    "Lines longer than this many characters are truncated when read returns them.",
  globMaxResults: "glob — max results",
  globMaxResultsDesc:
    "Maximum number of file paths the glob tool shows the model at once; the rest are written to disk and can be retrieved with the read tool.",
  grepMaxMatches: "grep — max matches",
  grepMaxMatchesDesc:
    "Maximum number of matches the grep tool shows the model at once; the rest are written to disk and can be retrieved with the read tool.",
  grepMaxLineLength: "grep — max line length",
  grepMaxLineLengthDesc:
    "Matched lines longer than this many characters are truncated when grep returns them.",
  grepMaxOutputBytes: "grep — max output bytes",
  grepMaxOutputBytesDesc:
    "Maximum total byte size of grep output handed to the model in one call; beyond this the output is truncated.",
  // ── reboot-confirm dialog (decision 3B) ──
  rebootTitle: "Restart required",
  rebootMessage:
    "The security configuration has been saved, but a restart is required for it to take effect. Restart now?",
  rebootConfirm: "Restart now",
  rebootCancel: "Later",
  rebootDeferred: "Saved. The change will take effect after the next restart.",
  // ── status ──
  saved: "Saved",
  saveFailed: "Save failed",
  add: "Add",
  remove: "Remove",
};

export default toolSafety;
