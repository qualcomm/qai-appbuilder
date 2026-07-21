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
//
// mbPro：Model Builder Pro 聊天卡片文案。configReview = MB Pro 的
// "配置确认"卡（ConfigReviewCard.vue，映射自 upstream 的
// config_review_needed 事件）。
// =============================================================================

const mbPro = {
  configReview: {
    title: "配置确认",
    countdownLabel: "剩余确认时间",
    platform: "平台",
    model: "模型",
    userConstraint: "用户约束",
    paths: "路径",
    params: "参数",
    inputPaths: "运行时输入路径",
    notebook: "Notebook",
    hint: "在下方输入「确认」开跑，或直接说明要改的参数。",
    hintExpired: "倒计时结束，如未回应系统将自动开跑；如需修改请在下方输入。",
  },
};

export default mbPro;
