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

const modelBuilder = {
  promote: {
    conflictDetected: "檢測到衝突 — 請選擇處理策略",
    defaultPrecision: "App Builder 中的預設精度",
    disabledReason: {
      exporting: "正在產生中，請稍候…",
      generic: "當前狀態無法產生 Pack，請檢查上方提示",
      noBins: "未找到 precision binary，請先在 Model Builder 中完成模型轉換（產生 <model>_<precision>.bin）",
      noDefaultVariant: "請為勾選的精度指定一個預設精度（下方 “預設精度” 一列）",
      noVariantSelected: "請至少勾選一個精度",
    },
    generate: "產生 App Builder Pack",
    generating: "產生中...",
    import: "匯入到 App Builder",
    importFailed: "匯入失敗",
    importSuccess: "已成功匯入到 App Builder",
    noBinsHint: "output/ 下未發現可識別的精度產物。期望格式：<model>_<precision>.bin（如 model_fp16.bin、model_fp32.bin、model_int8.bin）。",
    needsNormalize: {
      title: "模型已下載，但尚不可匯入",
      body: "在該工作區偵測到一個 AI Hub 模型，但它還沒有被規範化成 App Builder 所需的佈局。請讓 Agent 執行 Model Hub 的 Step 6.5 規範化（aihub_to_manifest.py）——它會產生 output/<model>_<precision>.{bin,dlc} + inference_manifest.json，之後即可匯入該模型。",
    },
    noCandidates: "未發現可匯入的 Pack 候選。請先在 Model Builder 中完成 Phase 7 生成候選包。",
    packGenerated: "Pack 已生成：{name}",
    policyBump: "升級版本號",
    policyCancel: "存在則取消",
    policyReplace: "替換已有",
    ready: "就緒",
    relativeTime: {
      hoursAgo: "{n} 小時前",
      justNow: "剛剛",
      minutesAgo: "{n} 分鐘前",
    },
    repickPrecision: "重新選擇精度",
    rollback: "回滾",
    rollbackSuccess: "已回滾到上一版本",
    scanBinsTitle: "在 output/ 下偵測到的精度",
    scanning: "正在掃描工作區中的模型精度變體…",
    sizeMB: "{n} MB",
    title: "匯入到應用構建器",
    readyBadgeAria: "偵測到可推送的模型，點擊查看",
    validate: "校驗",
    validationPassed: "校驗通過 — 可以匯入",
    suggestedVersion: "建議的下一個版本號：{v}",
    variantsCount: "已選 {n} 個精度",
    noWorkspace: "當前對話未偵測到模型工作區。請先在 Model Builder 中轉換一個模型。",
    workspaceFound: "已找到模型工作區：",
    warn: {
      provenance_failed: "模型精度驗證未通過 — REPORT.md 中未包含有效的 Cosine Similarity 數值。建議：執行推理驗證（對比 ONNX 基線與 QNN 輸出的餘弦相似度），將結果寫入 REPORT.md（格式：Cosine Similarity (ONNX vs FP16): 0.9999），然後重新匯出。",
      provenance_not_found: "未找到模型驗證記錄（REPORT.md 中缺少 Cosine Similarity 資料）。如需消除此警告，請在 REPORT.md 中補充推理驗證結果後重新匯出。",
    },
    // 偵測到當前對話的模型工作區裡存在可用的精度變體時，在編輯器上方浮出的
    // 提示條。本對話內依 workdir 記錄 dismiss（不做永久關閉），因為觸發
    // 條件本身已足夠收斂：真實磁碟狀態 + 允許模式 + 已辨識到工作區。
    readyNotice: {
      title: "模型可打包 🎉",
      // 中文無單複數變化：One / Many 值相同，保留兩條只是為滿足 i18n
      // 三語 schema 對齊（英文側有單複數差異）。
      descriptionOne: "偵測到 1 個精度變體，可以打包為 App Builder pack 一鍵重用。",
      descriptionMany: "偵測到 {count} 個精度變體，可以打包為 App Builder pack 一鍵重用。",
      action: "→ 匯出到 App Builder",
      dismiss: "稍後",
    },
  },
};

export default modelBuilder;
