// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// Canonical i18n message schema —— 类型真值源由 en 主入口推导。
//
// 设计：en（./en.ts）是 i18n 的"类型真值源"。MessageSchema = typeof en，
// 即 en 组装后的精确 per-key 结构（每个 leaf 推导为 string）。zh-CN / zh-TW
// 主入口以 `: MessageSchema` 约束，tsc 即可在编译期强制三语 key 结构完全一致
// （漏 key / 多 key / 拼错 key 都会编译报错），同时 `t('nav.chat')` 在调用点
// 获得静态校验与自动补全（经 ./schema.d.ts 的 vue-i18n 模块增强）。
//
// 新增 key：只需在 en 的对应 ./en/{ns}.ts 子文件里加，tsc 会要求 zh-CN /
// zh-TW 的同名子文件补齐同一 key（无需再手工维护一份独立的 interface）。
// =============================================================================
import type en from "./en";

export type MessageSchema = typeof en;
