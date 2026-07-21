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

const projectAccess = {
  confirmDisable: "停用專案存取後，AI 將無法讀取或修改專案檔案。是否繼續？",
  confirmEnable: "允許 AI 存取專案目錄：{path}？",
  description: "控制 AI 是否可以讀取和修改專案目錄中的檔案。",
  dialogs: {
    disableTitle: "停用專案存取",
    enableTitle: "啟用專案存取",
  },
  disableLabel: "AI 專案目錄存取已停用",
  disabledWarning: "專案目錄存取已停用。AI 無法讀取或修改專案檔案。",
  enableHint: "啟用後，AI 可以讀取、搜尋和修改設定的專案路徑中的檔案。",
  enableLabel: "允許 AI 存取專案目錄",
  notifications: {
    saveFailed: "儲存專案存取設定失敗",
    saved: "專案存取設定已儲存",
  },
  pathHint: "專案目錄的絕對路徑（例如 C:\\Users\\you\\MyProject）",
  pathLabel: "專案目錄路徑",
  pathPlaceholder: "C:\\Users\\you\\MyProject",
  resetSkipDirs: "恢復預設值",
  skipDirPlaceholder: "新增目錄名稱...",
  skipDirsEmpty: "未設定任何目錄",
  skipDirsHint: "glob/grep 工具自動略過的目錄名稱（如 venv、node_modules），可提升效能並減少無關結果。",
  skipDirsLabel: "略過的目錄",
  status: {
    disabled: "專案存取已停用",
    enabled: "專案存取已啟用",
  },
  title: "專案目錄存取",
};

export default projectAccess;
