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

const views = {
  channels: {
    description: "管理訊息頻道，連接外部平台",
    title: "頻道",
  },
  chat: {
    description: "多分頁對話工作區。分頁、串流輸出與工具呼叫將於 PR-054 接入。",
    placeholder: "對話工作區將顯示於此。",
    title: "聊天",
  },
  downloads: {
    description: "模型與資源下載。透過 SSE 串流上報進度。",
    title: "下載",
  },
  security: {
    description: "FileGuard 安全策略與權限管理",
    title: "安全",
  },
  service: {
    description: "本機服務守護程序 — 狀態、重啟、日誌。",
    title: "服務",
  },
  settings: {
    build_info_error: "載入建置資訊失敗。",
    build_info_heading: "建置資訊",
    build_info_loading: "正在載入建置資訊…",
    description: "應用程式偏好與建置資訊。",
    field_data_dir: "資料目錄",
    field_edition: "版本類別",
    field_name: "名稱",
    field_python_path: "Python 路徑",
    field_version: "版本",
    title: "設定",
  },
  skills: {
    description: "技能註冊與審核佇列。於 PR-053 與 Chat 一同實作。",
    title: "技能",
  },
};

export default views;
