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

const commandPalette = {
  action: {
    clearMessages: "清空当前对话",
    fontDecrease: "缩小字体",
    fontIncrease: "放大字体",
    fontReset: "重置字体大小",
    newConversation: "新建对话",
    openChannels: "打开 Channels",
    openChat: "打开 Chat",
    openDownloads: "打开 Downloads",
    openService: "打开 Service",
    openSettings: "打开 Settings",
    openSkills: "打开 Skills",
    toggleTheme: "切换主题",
  },
  group: {
    actions: "操作",
    models: "模型",
    skills: "技能",
  },
  hint: "按 Ctrl+K 打开命令面板",
  noResults: "未找到匹配的命令",
  placeholder: "输入命令...",
};

export default commandPalette;
