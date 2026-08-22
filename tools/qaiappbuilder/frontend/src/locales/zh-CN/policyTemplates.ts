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

const policyTemplates = {
  applied: "模板应用成功",
  apply: "应用",
  applyFailed: "模板应用失败",
  current: "当前",
  desc: "选择一个策略模板快速应用预定义的安全配置。",
  description: "应用预定义的安全策略模板。",
  empty: "无可用的策略模板。",
  title: "策略模板",
  clearTitle: "清空所有规则",
  clearHint:
    "移除全部规则，让通用允许级联（工作区 / 技能 / 自动审批 / 系统白名单 / 授权）驱动决策；未被这些覆盖的访问会走弹窗询问。",
  clearBtn: "清空规则",
  // 按内置模板 id 键入的分模板展示文案；渲染时按 id 查找，查不到回退后端文本。
  items: {
    demo: {
      name: "演示",
      description:
        "只允许 AI 读取项目目录；对项目内写入、执行以及项目外任何操作均弹窗询问。",
    },
    development: {
      name: "开发",
      description:
        "允许 AI 读写项目目录与临时目录；其他一切访问弹窗询问。日常开发推荐。",
    },
    strict: {
      name: "严格",
      description:
        "无模板规则；所有操作走通用允许级联，未覆盖的访问均需用户在弹窗中显式批准。最适合高安全性环境。",
    },
  },
};

export default policyTemplates;
