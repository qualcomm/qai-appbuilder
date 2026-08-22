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
    aiSearch: {
      button: "AI 搜尋權重",
      // 「掃到了」情境下的弱化文案：即使掃描有結果，入口也必須可達（掃到了
      // ≠ 掃對了——例如同一個 293MB 檔案被同時報成 FP16 與 FP32），但它不應
      // 該搶主操作「生成 App Builder Pack」的注意力。
      buttonVerify: "請 Agent 覆核",
      hint: "找不到權重檔案？讓本對話中的 Agent 在該工作區裡尋找並記錄結果——然後重新點擊「匯入」即可。",
      hintVerify: "結果不對？讓本對話中的 Agent 依磁碟實際情況覆核這個工作區的模型檔案。",
      cluesTitle: "掃描看到的情況",
      cluesSubdirs: "output/ 下的子目錄：",
      cluesRejected: "看到但被排除的檔案：",
      cluesRejectedRow: "{path} —— {size}（{reason}）",
      noDiagnostics: "（本次掃描沒有回傳診斷細節）",
      noPrecisions: "（未偵測到）",
      // 注入到目前聊天工作階段的提示詞。{workdir} 為模型工作目錄；
      // {outputDir} / {manifestPath} 是用工作目錄「自身的路徑分隔符號」預先
      // 組好的路徑（避免 Windows 工作目錄算繪出 `C:\a\b/output` 這種混用分隔
      // 符號的路徑）；{diagnostics} 是後端已算繪好的權重尋找報告（掃了哪些目
      // 錄 / 哪些檔案被拒 / 建議的下一步），讓 Agent 從證據出發而不必從零摸
      // 索；{manifestExample} 是字面 JSON 片段（程式碼字面量——vue-i18n 的訊
      // 息裡無法直接放裸 `{`）。
      prompt: "App Builder 匯入時未能在 `{outputDir}` 下找到可用的 NPU 權重，請幫我定位。\n\n後端權重尋找報告（證據——請先讀它，不要從零重新推導）：\n---\n{diagnostics}\n---\n\n請嚴格按以下四步執行：\n\n1. 只在有界路徑內尋找：`{workdir}` 及其 `output/` 子樹，深度最多 3 層。禁止對 `C:\\` 或 `C:\\WoS_AI` 根目錄做遞迴掃描——那會跑 30 分鐘以上並卡死。\n2. 目標是副檔名為 `.bin` 或 `.dlc` 且大小不小於 1 MiB 的檔案。0 位元組（或只有幾 KB）的是佔位檔，不是真實權重；真正的 context binary 常常位於 `bins/` 之類的子目錄中。\n3. 找到之後，寫入或修正 `{manifestPath}`，使其內容為：{manifestExample} —— `context_binary` 路徑必須相對於工作目錄、必須位於 `output/` 之內、副檔名必須是 `.bin` 或 `.dlc`、且不小於 1 MiB。可用精度 token：`fp16`、`fp32`、`bf16`、`w8a8`（int8）、`w8a16`、`w4a16`、`w4a8`（int4）、`w16a16`，或自訂的 `w<N>a<N>[b<N>]`。寫完後請告訴我在 App Builder 面板中重新點擊「匯入」。\n4. 如果確實沒有權重，請說明缺的是哪一步（多半是 context binary 的產生沒有完成），並給出下一步要執行的具體命令。",
      // 「已掃到但要覆核」情境使用的提示詞。上面那條以「未能找到可用權重」開
      // 頭，與畫面上已列出的精度自相矛盾，故另立一條。硬約束完全一致（有界
      // 尋找、禁止遞迴掃 `C:\` / `C:\WoS_AI` 根、`.bin`/`.dlc` 且 ≥1 MiB、寫
      // inference_manifest.json 的 precision + context_binary 兩欄位），另外
      // 補上出問題的那兩項核對：一個檔案不能被報成兩個精度、輔助元件不能被
      // 當成獨立精度。
      promptVerify: "App Builder 匯入面板目前為 `{workdir}` 列出了這些精度：{precisions}。我不確定這個列表是對的，請依磁碟實際情況幫我核對。\n\n請嚴格核對以下五點：\n\n1. 只在有界路徑內尋找：`{workdir}` 及其 `output/` 子樹，深度最多 3 層。禁止對 `C:\\` 或 `C:\\WoS_AI` 根目錄做遞迴掃描——那會跑 30 分鐘以上並卡死。\n2. 列出 `{outputDir}` 下所有副檔名為 `.bin` 或 `.dlc` 且不小於 1 MiB 的檔案及其真實大小。0 位元組（或只有幾 KB）的是佔位檔，不是真實權重。\n3. 確認列出的每個精度都對應一個「互不相同」的 context binary：兩個精度絕不能指向同一個檔案；輔助元件（小體積的 `flow.bin`、encoder/decoder 分片、`bins/` 裡的附屬檔案）也絕不能被當成一個獨立精度。\n4. 把該列表與 `{manifestPath}` 交叉核對。若與磁碟不一致，請寫入或修正該 manifest，使其內容為：{manifestExample} —— `context_binary` 路徑必須相對於工作目錄、必須位於 `output/` 之內、副檔名必須是 `.bin` 或 `.dlc`、且不小於 1 MiB。可用精度 token：`fp16`、`fp32`、`bf16`、`w8a8`（int8）、`w8a16`、`w4a16`、`w4a8`（int4）、`w16a16`，或自訂的 `w<N>a<N>[b<N>]`。\n5. 最後告訴我哪些精度是真實的、哪些是誤偵測的，以及我現在能否直接生成 App Builder Pack。",
    },
    needsNormalize: {
      title: "模型已下載，但尚不可匯入",
      body: "在該工作區偵測到一個 AI Hub 模型，但它還沒有被規範化成 App Builder 所需的佈局。請讓 Agent 執行 Model Hub 的 Step 6.5 規範化（aihub_to_manifest.py）——它會產生 output/<model>_<precision>.{'{'}bin,dlc{'}'} + inference_manifest.json，之後即可匯入該模型。",
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
  gallery: {
    title: "提交到模型畫廊",
    openGallery: "模型畫廊",
    tip: "提示：你可以讓 AI 生成 MODEL_CARD.md，總結轉換流程、參數和結果，供其他使用者參考複現。",
    generateModelCard: "生成 MODEL_CARD.md",
    modelCardExists: "工作區中已存在 MODEL_CARD.md",
    submitter: "提交者",
    email: "信箱",
    modelName: "模型名稱",
    category: "類別",
    categoryGenai: "GenAI（LLM/VLM/LVM）",
    categoryNonGenai: "Non-GenAI（CV/ASR/OCR）",
    modelType: "模型類型",
    customer: "客戶",
    customCustomer: "自訂客戶名稱",
    qnnVersion: "QNN 版本",
    quantMethod: "量化方法",
    scenario: "場景 / 用途",
    notebookUrl: "Notebook URL",
    description: "描述",
    filesTitle: "待提交檔案",
    selectAll: "全選",
    deselectAll: "取消全選",
    fileRequired: "必需",
    noDlcWarning: "必須選擇至少一個 .dlc 檔案",
    noFilesFound: "工作區中未發現模型產物，請先完成模型轉換。",
    filterPlaceholder: "篩選檔案...",
    openFile: "開啟檔案",
    deleteFile: "刪除檔案",
    confirmDelete: "確定要從磁碟永久刪除「{name}」嗎？此操作無法復原。路徑：{path}",
    collapse: "收合",
    expand: "展開",
    generateDesc: "生成",
    generatingDesc: "生成中…",
    generateDescHint: "讓雲端模型根據工作區文件（優先 MODEL_CARD.md）生成描述",
    selectedSummary: "已選 {count} 個檔案，共 {size}",
    missingSubmitter: "請填寫提交者姓名",
    missingEmail: "請填寫信箱地址",
    missingModelName: "請填寫模型名稱",
    scriptsWillZip: "Python 腳本（>{n} 個）將打包為 scripts.zip",
    submit: "提交",
    submitting: "提交中...",
    packagingFile: "正在打包 {name}（{done}/{total}）",
    uploadWaiting: "上傳中，等待伺服器回應...",
    uploadingModel: "正在上傳 {name}",
    minimize: "最小化",
    restore: "還原對話框",
    cancel: "取消",
    submitSuccess: "提交成功！Upload ID：{id}",
    submitFailed: "提交失敗：{reason}",
    checkStatus: "查看審核狀態",
    historyTitle: "提交歷史（{n}）",
    required: "此欄位為必填項",
    invalidEmail: "請輸入有效的信箱地址",
    scanning: "正在掃描工作區...",
    noWorkspace: "沒有可用的模型工作區，請先完成模型轉換。",
  },
};

export default modelBuilder;
