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

const input = {
  attachImage: "Attach image",
  blockedPlaceholder: "Another session is generating...",
  // Shown while THIS tab is still working (a live turn, or a sub-agent /
  // backgrounded command that outlived it). Both keys do something useful here
  // and behave differently, so the hint names both: Enter defers the message
  // until the tab is quiet, Ctrl+Enter folds it into the run that is happening
  // NOW. Not used in CC/OC sessions — they support neither key.
  busyPlaceholder:
    "Working... (Enter to queue for later · Ctrl+Enter to send into the current run)",
  chars: "{n} chars",
  imageAttached: "Image attached",
  pasteImage: "Paste image",
  placeholder: "Send a message... (Enter to send · Shift+Enter for newline)",
  removeImage: "Remove image",
  streamingPlaceholder: "Generating response...",
};

export default input;
