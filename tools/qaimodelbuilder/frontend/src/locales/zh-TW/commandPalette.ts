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
    clearMessages: "清空目前對話",
    fontDecrease: "縮小字型",
    fontIncrease: "放大字型",
    fontReset: "重設字型大小",
    newConversation: "新建對話",
    openChannels: "開啟 Channels",
    openChat: "開啟 Chat",
    openDownloads: "開啟 Downloads",
    openService: "開啟 Service",
    openSettings: "開啟 Settings",
    openSkills: "開啟 Skills",
    toggleTheme: "切換主題",
  },
  group: {
    actions: "操作",
    models: "模型",
    skills: "技能",
  },
  hint: "按 Ctrl+K 開啟指令面板",
  noResults: "未找到符合的指令",
  placeholder: "輸入指令...",
};

export default commandPalette;
