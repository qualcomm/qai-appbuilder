// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。
//
// 结构必须与 en/qgenieQuota.ts 完全一致（key 一一对应），由 locale parity
// 测试 + tsc 强制。修改时严守 AGENTS.md §8 文件编码铁律。
// =============================================================================

// QGenie 双桶配额 —— 侧边栏用量条 + 配额耗尽提示。
//
// class.Api / class.UI 按 QGenie 的 wire 值命名，但显示其控制台使用的标签，
// 使用户在此看到的与 QGenie 网页端一致。
const qgenieQuota = {
  label: "QGenie 配额",
  class: {
    Api: "API/SDK",
    UI: "IDE/CLI",
  },
  inUse: "当前使用",
  daily: "当日",
  weekly: "本周",
  full: "已用尽",
  unknown: "未知",
  staleBadge: "缓存",
  fetchedAt: "数据时间 {when}",
  staleAt: "缓存于 {when}（刷新失败）",
  // 必须写明：QGenie 无法按模型拆分花费（其 observe-only 写入器不存储模型
  // 标识）。不说明的话，用户会误以为这是当前模型的花费。
  costHeading: "QGenie 全部模型总花费（非仅当前模型）",
  costToday: "今日",
  costMonth: "本月",
  // 点击用量条弹出的窗口：tooltip 只能描述当前模型，逐模型的当日数据放这里。
  dialogTitle: "QGenie 当日配额",
  colModel: "模型",
  colClass: "配额类别",
  colUsedToday: "当日已用",
  colDailyLimit: "当日总量",
  selected: "当前",
  showIdle: "显示 {count} 个未使用的模型",
  hideIdle: "隐藏未使用的模型",
  close: "关闭",
  // 阶段式提醒：第一次跨线是建议性的（剩下的几个百分点仍是数百万 tokens），
  // 第二次是断流前的最后一次机会。
  finalTitle: "{class} 配额即将耗尽",
  finalBody:
    "{model} 的当日 {class} 配额即将用尽，下一次回复可能在中途被截断。",
  keepGoing: "继续使用",
  pickAnotherModel: "换一个模型",
  remainingTokens: "当日剩余 {count}",
  clickToSwitch: "点击后将后续请求计入 {class}",
  // 用量条无数据（或仅有旧数据）的原因。每条都指明对应的处理动作——
  // 单说“没有数据”对用户毫无帮助。
  reason: {
    no_api_key: "未设置 QGenie API Key —— 请在 设置 → 云端模型 中填写。",
    rate_limited:
      "QGenie 正在限流配额查询（约每分钟一次），显示的数字可能略旧。",
    unreachable: "无法连接 QGenie，请检查网络或 VPN。",
    not_configured: "此版本未配置 QGenie。",
    bad_base_url: "QGenie provider 的 base URL 不可用。",
    unknown: "无法读取配额（{code}）。",
  },
  // 过滤与手动同步。
  filterPlaceholder: "过滤模型…",
  noMatch: "没有匹配「{query}」的模型。",
  sync: "同步",
  syncing: "同步中…",
  syncTooltip: "立即获取最新数据。QGenie 对此约限流为每分钟一次。",
  switchTitle: "{class} 配额即将用尽",
  switchBody:
    "{model} 的当日 {class} 配额已使用 {percent}%。是否将后续请求切换到 {other}？",
  switchConfirm: "切换到 {other}",
  switchCancel: "继续使用 {class}",
  switchedNotice: "已切换到 {other} 配额。",
  bothTitle: "QGenie 两类配额均已用尽",
  bothBody:
    "{model} 的 API/SDK 与 IDE/CLI 当日配额都已用尽。请选择其它模型——切换配额类别无法解决。",
  bothConfirm: "知道了",
  interruptedTitle: "回复被中断：配额已用尽",
  interruptedBody:
    "{model} 的当日 {class} 配额在回复过程中用尽。上方已生成的内容会保留。可切换配额类别后重新提问，或改用其它模型。",
};

export default qgenieQuota;
