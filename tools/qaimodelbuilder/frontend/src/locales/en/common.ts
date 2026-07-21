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

const common = {
  actions: "Actions",
  add: "Add",
  all: "All",
  back: "Back",
  cancel: "Cancel",
  close: "Close",
  collapse: "Collapse",
  coming_soon: "Coming soon.",
  confirm: "Confirm",
  copied: "Copied",
  copy: "Copy",
  delete: "Delete",
  deselectAll: "Deselect All",
  disabled: "Disabled",
  dismiss: "Dismiss",
  edit: "Edit",
  empty: "Nothing here yet.",
  enabled: "Enabled",
  expand: "Expand",
  gotIt: "Got it",
  // Shared help-manual overlay (see components/common/HelpButton.vue).
  // Kept nested under `common` (not the top-level `help.*` namespace, which
  // holds CLI-command help copy) so it is stable across features.
  // Note: the modal close button reuses top-level `common.close` (the
  // ChannelInfoDialog chrome consumes that key), so no `close` entry is
  // duplicated here.
  help: {
    button: {
      ariaLabel: "Help",
    },
    loadFailed: "Failed to load help content. Please retry or check the official docs.",
    title: "Help",
    viewOfficial: "View official docs",
  },
  less: "Less",
  loading: "Loading…",
  minimize: "Minimize",
  more: "More",
  next: "Next",
  no: "No",
  noData: "No data",
  none: "None",
  not_implemented: "Not implemented yet.",
  off: "Off",
  on: "On",
  previous: "Previous",
  refresh: "Refresh",
  remove: "Remove",
  rename: "Rename",
  reset: "Reset",
  retry: "Retry",
  save: "Save",
  saving: "Saving...",
  search: "Search",
  selectAll: "Select All",
  status: "Status",
  submitting: "Submitting...",
  testing: "Testing...",
  unknown: "Unknown",
  unknownError: "An unknown error occurred",
  unnamed: "Unnamed",
  yes: "Yes",
};

export default common;
