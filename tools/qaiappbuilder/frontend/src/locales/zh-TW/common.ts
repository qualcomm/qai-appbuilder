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
  actions: "操作",
  add: "新增",
  all: "全部",
  back: "返回",
  cancel: "取消",
  close: "關閉",
  collapse: "摺疊",
  coming_soon: "即將推出。",
  confirm: "確認",
  copied: "已複製",
  copy: "複製",
  delete: "刪除",
  deselectAll: "取消全選",
  disabled: "已停用",
  dismiss: "忽略",
  edit: "編輯",
  empty: "暫無內容。",
  enabled: "已啟用",
  expand: "展開",
  gotIt: "知道了",
  // Shared help-manual overlay (see components/common/HelpButton.vue).
  // Kept nested under `common` (not the top-level `help.*` namespace, which
  // holds CLI-command help copy) so it is stable across features.
  // Note: the modal close button reuses top-level `common.close` (the
  // ChannelInfoDialog chrome consumes that key), so no `close` entry is
  // duplicated here.
  help: {
    button: {
      ariaLabel: "說明",
    },
    loadFailed: "說明內容載入失敗，請稍後再試或查看官方文件。",
    title: "說明",
    viewOfficial: "查看官方文件",
  },
  less: "收起",
  loading: "載入中…",
  minimize: "最小化",
  more: "更多",
  next: "下一步",
  no: "否",
  noData: "暫無資料",
  none: "無",
  not_implemented: "尚未實作。",
  off: "關",
  on: "開",
  previous: "上一步",
  refresh: "重新整理",
  remove: "移除",
  rename: "重新命名",
  reset: "重設",
  retry: "重試",
  save: "儲存",
  saving: "儲存中...",
  search: "搜尋",
  selectAll: "全選",
  status: "狀態",
  submitting: "提交中...",
  testing: "測試中...",
  unknown: "未知",
  unknownError: "發生未知錯誤",
  unnamed: "未命名",
  yes: "是",
};

export default common;
