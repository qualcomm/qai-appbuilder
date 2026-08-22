// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。
//
// 真值源说明：本项目 i18n 已无自动生成管道。本文件就是当前唯一真值源，
// 必须手工维护。修改时严守 AGENTS.md §8 文件编码铁律（UTF-8，禁止
// GBK/CP437 等非 UTF-8 编码，禁止双重编码损坏）。
//
// 类型：en/{ns}.ts 经主入口 en.ts 组装后由 typeof 推导出 MessageSchema；
// zh-CN / zh-TW 的同名子文件须保持与 en 完全一致的 key 结构。
// =============================================================================

// QGenie dual-bucket quota — the sidebar gauge + the exhaustion prompts.
//
// `class.Api` / `class.UI` are keyed by QGenie's own wire values but shown with
// the labels its console uses, so what the user reads here matches what they
// see in the QGenie web UI.
const qgenieQuota = {
  label: "QGenie Quota",
  class: {
    Api: "API/SDK",
    UI: "IDE/CLI",
  },
  inUse: "in use",
  daily: "Daily",
  weekly: "Weekly",
  full: "exhausted",
  unknown: "unknown",
  staleBadge: "cached",
  fetchedAt: "As of {when}",
  staleAt: "Cached at {when} (refresh failed)",
  // Spelled out because QGenie cannot attribute spend per model: its
  // observe-only writer does not store model identifiers. Without saying so,
  // a reader takes this for the selected model's cost.
  costHeading: "Total spend, ALL QGenie models (not just this one)",
  costToday: "Today",
  costMonth: "This month",
  // Click-through dialog: the tooltip can only describe the selected model, so
  // the per-model daily figures live here.
  dialogTitle: "QGenie daily quota",
  colModel: "Model",
  colClass: "Quota class",
  colUsedToday: "Used today",
  colDailyLimit: "Daily total",
  selected: "selected",
  showIdle: "Show {count} unused models",
  hideIdle: "Hide unused models",
  close: "Close",
  // Escalating warning: the first crossing is advisory (the remaining few
  // percent are still millions of tokens), the second is the last chance before
  // a reply gets cut off mid-sentence.
  finalTitle: "{class} quota about to run out",
  finalBody:
    "{model} has nearly exhausted its daily {class} allowance. The next reply may be cut off mid-sentence.",
  keepGoing: "Keep using it",
  pickAnotherModel: "Pick another model",
  remainingTokens: "{count} left today",
  clickToSwitch: "Click to bill subsequent requests to {class}",
  // Why the gauge has nothing (or only stale data) to show. Each names the
  // action that fixes it, since "no gauge" alone tells the user nothing.
  reason: {
    no_api_key: "No QGenie API key set — add one in Settings → Cloud Models.",
    rate_limited:
      "QGenie is rate-limiting quota lookups (about once a minute). Figures may be a little old.",
    unreachable: "Could not reach QGenie. Check your network or VPN.",
    not_configured: "QGenie is not configured in this build.",
    bad_base_url: "The QGenie provider's base URL is not usable.",
    unknown: "Quota could not be read ({code}).",
  },
  // Filter + manual sync.
  filterPlaceholder: "Filter models…",
  noMatch: 'No model matches "{query}".',
  sync: "Sync",
  syncing: "Syncing…",
  syncTooltip:
    "Fetch the latest figures now. QGenie throttles this to about once a minute.",
  switchTitle: "{class} quota almost spent",
  switchBody:
    "{model} has used {percent}% of its daily {class} allowance. Switch to {other} for subsequent requests?",
  switchConfirm: "Switch to {other}",
  switchCancel: "Stay on {class}",
  switchedNotice: "Switched to {other} quota.",
  bothTitle: "Both QGenie quotas spent",
  bothBody:
    "{model} has exhausted both its API/SDK and IDE/CLI daily allowances. Pick another model — switching quota class will not help.",
  bothConfirm: "Got it",
  interruptedTitle: "Reply cut short: quota exhausted",
  interruptedBody:
    "The daily {class} allowance for {model} ran out mid-reply. The partial answer above is kept. Switch quota class and ask again, or pick another model.",
};

export default qgenieQuota;
