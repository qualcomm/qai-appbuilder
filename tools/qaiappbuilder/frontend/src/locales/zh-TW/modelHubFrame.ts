// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（無 BOM）。
// zh-TW 版：與 en/modelHubFrame.ts key 結構完全一致（parity 由 tsc/測試強制）。
// =============================================================================

import type { MessageSchema } from "../schema";

const modelHubFrame: MessageSchema["modelHubFrame"] = {
  // 1. AI Hub 模型選擇
  pickModel: "選擇模型",
  pickModelHint: "搜尋並選擇一個高通 AI Hub 預編譯模型",
  pickModelHeader: "選擇 AI Hub 模型",
  pickModelPlaceholder: "例如 resnet50、whisper-base、zipformer",
  pickModelConfirm: "確認",
  pickModelDesc:
    "輸入 AI Hub 模型名稱，Agent 會下載預編譯模型包並在端側直接執行——無需轉換。",
  pickModelFilledPrompt:
    "從 Qualcomm AI Hub 下載預編譯的「{model}」模型，然後在 NPU 上用一個範例輸入執行並展示結果。請一次跑完整個流程。",
  // 2. 推送到 App Builder（與 Model Builder 用詞統一——兩個模組產出相同的
  // app_pack，動作用詞應完全一致）
  export: "推送到 App Builder",
  exportTitle: "將此 AI Hub 模型推送為可直接匯入 App Builder 的模型包",
  exportHeader: "推送到 App Builder",
  exportDesc:
    "推理驗證通過後，Agent 會將模型打包（與 Model Builder 相同的 app_pack 契約），即可匯入 App Builder。",
};

export default modelHubFrame;
