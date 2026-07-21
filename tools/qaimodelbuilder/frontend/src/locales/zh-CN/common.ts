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
  add: "添加",
  all: "全部",
  back: "返回",
  cancel: "取消",
  close: "关闭",
  collapse: "折叠",
  coming_soon: "即将推出。",
  confirm: "确认",
  copied: "已复制",
  copy: "复制",
  delete: "删除",
  deselectAll: "取消全选",
  disabled: "已禁用",
  dismiss: "忽略",
  edit: "编辑",
  empty: "暂无内容。",
  enabled: "已启用",
  expand: "展开",
  gotIt: "知道了",
  // Shared help-manual overlay (see components/common/HelpButton.vue).
  // Kept nested under `common` (not the top-level `help.*` namespace, which
  // holds CLI-command help copy) so it is stable across features.
  // Note: the modal close button reuses top-level `common.close` (the
  // ChannelInfoDialog chrome consumes that key), so no `close` entry is
  // duplicated here.
  help: {
    button: {
      ariaLabel: "帮助",
    },
    loadFailed: "帮助内容加载失败，请稍后再试或查看官方文档。",
    title: "帮助",
    viewOfficial: "查看官方文档",
  },
  less: "收起",
  loading: "加载中…",
  minimize: "最小化",
  more: "更多",
  next: "下一步",
  no: "否",
  noData: "暂无数据",
  none: "无",
  not_implemented: "暂未实现。",
  off: "关",
  on: "开",
  previous: "上一步",
  refresh: "刷新",
  remove: "移除",
  rename: "重命名",
  reset: "重置",
  retry: "重试",
  save: "保存",
  saving: "保存中...",
  search: "搜索",
  selectAll: "全选",
  status: "状态",
  submitting: "提交中...",
  testing: "测试中...",
  unknown: "未知",
  unknownError: "发生未知错误",
  unnamed: "未命名",
  yes: "是",
};

export default common;
