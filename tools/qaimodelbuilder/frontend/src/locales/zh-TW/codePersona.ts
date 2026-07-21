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

const codePersona = {
  activeBadge: "目前",
  alreadyDefaultHint: "目前已是預設值",
  architect: {
    desc: "動手寫程式前先完成拆解與設計",
    name: "方案規劃",
  },
  ask: {
    desc: "講解概念、分析程式碼、給出建議",
    name: "答疑解釋",
  },
  cancel: "取消",
  code: {
    desc: "撰寫、修改與重構程式碼",
    name: "編碼實作",
  },
  createFailed: "建立角色失敗",
  customizedHint: "已自訂（點擊可編輯）",
  customizedTag: "已自訂",
  deleteFailed: "刪除模式失敗",
  deleteNotSupported: "伺服器不支援刪除",
  debugger: {
    desc: "系統化地定位並修復問題",
    name: "排錯診斷",
  },
  discardConfirm: "目前有未儲存的修改，確定要放棄嗎？",
  editPrompts: "編輯提示詞…",
  groups: {
    command: "命令執行",
    commandDesc: "執行 Shell 命令和背景程序",
    edit: "檔案編輯",
    editDesc: "建立、修改和修補檔案",
    label: "工具權限",
    read: "檔案讀取",
    readDesc: "讀取檔案、搜尋內容、瀏覽網頁",
    resetGroups: "重設權限",
    restrictedHint: "限制範圍：{pattern}",
    other: "其它",
  },
  loadFailed: "載入編程模式失敗",
  loading: "正在載入…",
  menuTitle: "編程模式",
  noModesFound: "未找到編程模式。",
  orchestrator: {
    desc: "把大任務拆成可獨立完成的子任務",
    name: "任務協調",
  },
  optimizer: {
    desc: "重構與最佳化程式碼，提升可讀性與效能",
    name: "重構最佳化",
  },
  promptLabel: "系統提示詞",
  promptPlaceholder: "輸入該模式下使用的系統提示詞。留空或點擊「恢復預設」即可還原。",
  reload: "重新載入",
  reloadHint: "從伺服器重新載入（捨棄本地草稿）",
  removed: "模式已刪除",
  removeBtn: "刪除",
  removeTitle: "刪除此模式",
  resetConfirm: "確定要將「{name}」恢復為預設提示詞嗎？你的自訂內容將被清除。",
  resetFailed: "恢復失敗",
  resetSuccess: "「{name}」已恢復預設",
  resetToDefault: "恢復預設",
  reviewer: {
    desc: "審查程式碼的正確性、風格與可維護性",
    name: "程式碼審查",
  },
  save: "儲存",
  saveFailed: "儲存失敗",
  saveSuccess: "「{name}」已儲存",
  selectFailed: "選擇角色失敗",
  settingsDesc: "為不同編程模式自訂系統提示詞。僅在使用雲端模型且選中「編程」功能時生效。修改按模式儲存，重啟不會遺失。",
  settingsTitle: "編程模式提示詞",
  switchFailed: "切換編程模式失敗",
  unsavedChanges: "有未儲存的修改",
};

export default codePersona;
