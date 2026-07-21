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

const sessionWorkspace = {
  actionLabel: "設定工作目錄",
  cancel: "取消",
  confirm: "儲存",
  defaultLabel: "預設",
  headerLabel: "工作目錄",
  headerTitle: "設定目前對話的工作目錄",
  hint: "留空則使用全域預設目錄（C:\\WoS_AI）。本工作階段內 AI 可在此目錄讀寫，無需重複授權。",
  placeholder: "例如 C:\\WoS_AI\\my-project",
  pillTitle: "對話工作目錄 — 點擊修改",
  saveFailed: "設定工作目錄失敗",
  saving: "儲存中...",
  title: "對話工作目錄",
};

export default sessionWorkspace;
