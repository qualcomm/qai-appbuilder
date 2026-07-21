// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。
// zh-CN 版：与 en/modelHubFrame.ts key 结构完全一致（parity 由 tsc/测试强制）。
// =============================================================================

import type { MessageSchema } from "../schema";

const modelHubFrame: MessageSchema["modelHubFrame"] = {
  // 1. AI Hub 模型选择
  pickModel: "选择模型",
  pickModelHint: "搜索并选择一个高通 AI Hub 预编译模型",
  pickModelHeader: "选择 AI Hub 模型",
  pickModelPlaceholder: "例如 resnet50、whisper-base、yolov8-det",
  pickModelConfirm: "确认",
  pickModelDesc:
    "输入 AI Hub 模型名称，Agent 会下载预编译模型包并在端侧直接运行——无需转换。",
  pickModelFilledPrompt:
    "从 Qualcomm AI Hub 下载预编译的“{model}”模型，然后在 NPU 上用一个样例输入运行并展示结果。请一次性跑完整个流程。",
  // 2. 推送到 App Builder（与 Model Builder 用词统一——两个模块产出相同的
  // app_pack，动作用词应完全一致）
  export: "推送到 App Builder",
  exportTitle: "将此 AI Hub 模型推送为可直接导入 App Builder 的模型包",
  exportHeader: "推送到 App Builder",
  exportDesc:
    "推理验证通过后，Agent 会将模型打包（与 Model Builder 相同的 app_pack 契约），即可导入 App Builder。",
};

export default modelHubFrame;
