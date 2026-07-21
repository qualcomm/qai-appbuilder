// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工維護，UTF-8（無 BOM）。
// zh-TW 版：與 en/modeIntro.ts key 結構完全一致（parity 由 tsc/測試強制）。
// 繁體不 fallback，逐條獨立翻譯。
// =============================================================================

import type { MessageSchema } from "../schema";

const modeIntro: MessageSchema["modeIntro"] = {
  dontShowAgain: "不再顯示此提示",
  triggerLabel: "指引",
  appBuilder: {
    title: "App Builder 快速指引",
    subtitle: "把已訓練好的模型 + 前後處理邏輯封裝為可安裝的 App Builder pack。",
    step1: "在模型選單中勾選要用來建置的已匯入模型。",
    step2: "描述你希望這個 App 做什麼，Agent 會自動搭好程式骨架。",
    step3: "打開「我的 Apps」執行、打包或刪除已產生的應用。",
    chipMyApps: "打開我的 Apps",
    chipPromote: "推送到 App Builder",
  },
  gomaster: {
    title: "GoMaster 快速指引",
    subtitle: "雲端驅動的一鍵 ONNX 最佳化：上傳 → 最佳化 → 下載。",
    step1: "點擊「開始最佳化」，選擇你的 ONNX 模型檔案。",
    step2: "任務在雲端執行，進度會即時推送到抽屜中。",
    step3: "完成後即可下載最佳化後的模型與效能報告。",
    chipOptimize: "開始最佳化",
  },
  modelBuilder: {
    title: "Model Builder 快速指引",
    subtitle: "把來源模型轉換為 QNN/SNPE 執行包並推送到 App Builder。",
    step1: "上傳來源模型檔案，或指定一個模型工作目錄。",
    step2: "選擇量化精度（fp16 / w8a8 等）。",
    step3: "使用「推送到 App Builder」將轉換後的 pack 註冊給 App Builder 使用。",
    chipPromote: "推送到 App Builder",
    emptyExamplesTitle: "試試這些",
    ex1Label: "把 ONNX 模型轉換為 w8a8",
    ex1Prompt: "把我的 ONNX 模型轉換為 QNN DLC context binary，精度用 w8a8，然後在 NPU 上驗證推理。如果需要模型路徑請向我詢問。",
    ex2Label: "量化並對比精度",
    ex2Prompt: "把我的模型分別轉換為 fp16 和 w8a8，用一個範例輸入各跑一次推理，並回報兩者輸出的餘弦相似度，讓我判斷精度損失。",
    ex3Label: "把已轉換的模型匯出到 App Builder",
    ex3Prompt: "我的工作目錄裡已經有一個轉換好的模型，請把它打包成 app_pack 並推送到 App Builder，方便我在其上建構應用。",
  },
  modelHub: {
    title: "模型市場 快速指引",
    subtitle: "從 Qualcomm AI Hub 下載預編譯模型，一鍵推送到 App Builder。",
    step1: "在 Qualcomm AI Hub 中瀏覽或搜尋預編譯模型。",
    step2: "將模型包直接下載到你的工作目錄。",
    step3: "使用「推送到 App Builder」把它註冊給 App Builder 使用。",
    chipPromote: "推送到 App Builder",
    emptyExamplesTitle: "試試這些",
    ex1Label: "下載 ResNet50 並分類圖片",
    ex1Prompt: "從 Qualcomm AI Hub 下載預編譯的 ResNet50 圖像分類模型，然後在 NPU 上對一張測試圖片做分類推理並列印 Top-5 結果。請一次跑完整個流程。",
    ex2Label: "下載 YOLOv8 做目標偵測",
    ex2Prompt: "從 Qualcomm AI Hub 下載預編譯的 YOLOv8 目標偵測模型，在 NPU 上對一張範例圖片做偵測並畫出偵測框。",
    ex3Label: "下載模型並推送到 App Builder",
    ex3Prompt: "從 Qualcomm AI Hub 下載一個預編譯的圖像分類模型（如 Inception v3），在 NPU 上驗證推理通過後，把它打包並推送到 App Builder。",
  },
  pro: {
    title: "增強模式（Pro）快速指引",
    subtitle: "把繁重的模型轉換任務交給遠端 GPU Agent 自動完成。",
    step1: "點「設定」設定遠端 GPU Agent 的位址、帳號與埠號。",
    step2: "點「連線」——系統會自動挑一台閒置機器建立工作階段。",
    step3: "像日常聊天一樣描述轉換需求，Agent 會自動接手全流程。",
    chipSettings: "打開設定",
    chipConnect: "連線 GPU Agent",
  },
  code: {
    title: "程式碼模式快速指引",
    subtitle: "讓模型讀懂你的程式碼，協助你分析、修改、產生程式碼。支援任何 OpenAI 相容 API。",
    step1: "選一個「專家角色」（Persona），決定模型的思考重點。",
    step2: "上傳檔案或貼上 Git 儲存庫網址，讓模型看到程式碼脈絡。",
    step3: "在對話框中描述任務，模型會讀程式碼並給出修改建議或修補檔。",
    chipPersona: "選擇專家角色",
    chipContext: "上傳程式碼 / 儲存庫",
  },
};

export default modeIntro;
