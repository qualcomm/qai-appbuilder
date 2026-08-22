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
    conflictDetected: "检测到冲突 — 请选择处理策略",
    defaultPrecision: "App Builder 中的默认精度",
    disabledReason: {
      exporting: "正在生成中，请稍候…",
      generic: "当前状态无法生成 Pack，请检查上方提示",
      noBins: "未找到 precision binary，请先在 Model Builder 中完成模型转换（生成 <model>_<precision>.bin）",
      noDefaultVariant: "请为选中的精度指定一个默认精度（下方 “默认精度” 一栏）",
      noVariantSelected: "请至少勾选一个精度",
    },
    generate: "生成 App Builder Pack",
    generating: "生成中...",
    import: "导入到 App Builder",
    importFailed: "导入失败",
    importSuccess: "已成功导入到 App Builder",
    noBinsHint: "output/ 下未发现可识别的精度产物。期望格式：<model>_<precision>.bin（如 model_fp16.bin、model_fp32.bin、model_int8.bin）。",
    aiSearch: {
      button: "AI 搜索权重",
      // “扫到了”语境下的弱化文案：即使扫描有结果，入口也必须可达（扫到了 ≠
      // 扫对了——例如同一个 293MB 文件被同时报成 FP16 与 FP32），但它不应该
      // 抢主操作“生成 App Builder Pack”的注意力。
      buttonVerify: "请 Agent 复核",
      hint: "找不到权重文件？让本对话中的 Agent 在该工作区里查找并记录结果——然后重新点击“导入”即可。",
      hintVerify: "结果不对？让本对话中的 Agent 按磁盘实际情况复核这个工作区的模型文件。",
      cluesTitle: "扫描看到的情况",
      cluesSubdirs: "output/ 下的子目录：",
      cluesRejected: "看到但被排除的文件：",
      cluesRejectedRow: "{path} —— {size}（{reason}）",
      noDiagnostics: "（本次扫描没有返回诊断细节）",
      noPrecisions: "（未检出）",
      // 注入到当前聊天会话的提示词。{workdir} 为模型工作目录；{outputDir} /
      // {manifestPath} 是用工作目录“自身的路径分隔符”预先拼好的路径（避免
      // Windows 工作目录渲染出 `C:\a\b/output` 这种混用分隔符的路径）；
      // {diagnostics} 是后端已渲染好的权重查找报告（扫了哪些目录 / 哪些文件被
      // 拒 / 建议的下一步），让 Agent 从证据出发而不必从零摸索；
      // {manifestExample} 是字面 JSON 片段（代码字面量——vue-i18n 的消息里无法
      // 直接放裸 `{`）。
      prompt: "App Builder 导入时未能在 `{outputDir}` 下找到可用的 NPU 权重，请帮我定位。\n\n后端权重查找报告（证据——请先读它，不要从零重新推导）：\n---\n{diagnostics}\n---\n\n请严格按以下四步执行：\n\n1. 只在有界路径内查找：`{workdir}` 及其 `output/` 子树，深度最多 3 层。禁止对 `C:\\` 或 `C:\\WoS_AI` 根目录做递归扫描——那会跑 30 分钟以上并卡死。\n2. 目标是扩展名为 `.bin` 或 `.dlc` 且大小不小于 1 MiB 的文件。0 字节（或只有几 KB）的是占位符，不是真实权重；真正的 context binary 常常位于 `bins/` 之类的子目录中。\n3. 找到之后，写入或修正 `{manifestPath}`，使其内容为：{manifestExample} —— `context_binary` 路径必须相对于工作目录、必须位于 `output/` 之内、后缀必须是 `.bin` 或 `.dlc`、且不小于 1 MiB。可用精度 token：`fp16`、`fp32`、`bf16`、`w8a8`（int8）、`w8a16`、`w4a16`、`w4a8`（int4）、`w16a16`，或自定义的 `w<N>a<N>[b<N>]`。写完后请告诉我在 App Builder 面板中重新点击“导入”。\n4. 如果确实没有权重，请说明缺的是哪一步（多半是 context binary 的生成没有完成），并给出下一步要执行的具体命令。",
      // “已扫到但要复核”语境下使用的提示词。上面那条以“未能找到可用权重”开
      // 头，与屏幕上已列出的精度自相矛盾，故单独一条。硬约束完全一致（有界
      // 查找、禁止递归扫 `C:\` / `C:\WoS_AI` 根、`.bin`/`.dlc` 且 ≥1 MiB、写
      // inference_manifest.json 的 precision + context_binary 两字段），另外
      // 补上出问题的那两项核对：一个文件不能被报成两个精度、辅助组件不能被
      // 当成独立精度。
      promptVerify: "App Builder 导入面板目前为 `{workdir}` 列出了这些精度：{precisions}。我不确定这个列表是对的，请按磁盘实际情况帮我核对。\n\n请严格核对以下五点：\n\n1. 只在有界路径内查找：`{workdir}` 及其 `output/` 子树，深度最多 3 层。禁止对 `C:\\` 或 `C:\\WoS_AI` 根目录做递归扫描——那会跑 30 分钟以上并卡死。\n2. 列出 `{outputDir}` 下所有扩展名为 `.bin` 或 `.dlc` 且不小于 1 MiB 的文件及其真实大小。0 字节（或只有几 KB）的是占位符，不是真实权重。\n3. 确认列出的每个精度都对应一个“互不相同”的 context binary：两个精度绝不能指向同一个文件；辅助组件（小体积的 `flow.bin`、encoder/decoder 分片、`bins/` 里的附属文件）也绝不能被当成一个独立精度。\n4. 把该列表与 `{manifestPath}` 交叉核对。若与磁盘不一致，请写入或修正该 manifest，使其内容为：{manifestExample} —— `context_binary` 路径必须相对于工作目录、必须位于 `output/` 之内、后缀必须是 `.bin` 或 `.dlc`、且不小于 1 MiB。可用精度 token：`fp16`、`fp32`、`bf16`、`w8a8`（int8）、`w8a16`、`w4a16`、`w4a8`（int4）、`w16a16`，或自定义的 `w<N>a<N>[b<N>]`。\n5. 最后告诉我哪些精度是真实的、哪些是误检出的，以及我现在能否直接生成 App Builder Pack。",
    },
    needsNormalize: {
      title: "模型已下载，但尚不可导入",
      body: "在该工作区检测到一个 AI Hub 模型，但它还没有被规范化成 App Builder 所需的布局。请让 Agent 执行 Model Hub 的 Step 6.5 规范化（aihub_to_manifest.py）——它会生成 output/<model>_<precision>.{'{'}bin,dlc{'}'} + inference_manifest.json，之后即可导入该模型。",
    },
    noCandidates: "未发现可导入的 Pack 候选。请先在 Model Builder 中完成 Phase 7 生成候选包。",
    packGenerated: "Pack 已生成：{name}",
    policyBump: "升级版本号",
    policyCancel: "存在则取消",
    policyReplace: "替换已有",
    ready: "就绪",
    relativeTime: {
      hoursAgo: "{n} 小时前",
      justNow: "刚刚",
      minutesAgo: "{n} 分钟前",
    },
    repickPrecision: "重新选择精度",
    rollback: "回滚",
    rollbackSuccess: "已回滚到上一版本",
    scanBinsTitle: "在 output/ 下检测到的精度",
    scanning: "正在扫描工作区中的模型精度变体…",
    sizeMB: "{n} MB",
    title: "导入到应用构建器",
    readyBadgeAria: "检测到可推送的模型，点击查看",
    validate: "校验",
    validationPassed: "校验通过 — 可以导入",
    suggestedVersion: "建议的下一个版本号：{v}",
    variantsCount: "已选 {n} 个精度",
    noWorkspace: "当前对话未检测到模型工作区。请先在 Model Builder 中转换一个模型。",
    workspaceFound: "已找到模型工作区：",
    warn: {
      provenance_failed: "模型精度验证未通过 — REPORT.md 中未包含有效的 Cosine Similarity 数值。建议：运行推理验证（对比 ONNX 基线与 QNN 输出的余弦相似度），将结果写入 REPORT.md（格式：Cosine Similarity (ONNX vs FP16): 0.9999），然后重新导出。",
      provenance_not_found: "未找到模型验证记录（REPORT.md 中缺少 Cosine Similarity 数据）。如需消除此警告，请在 REPORT.md 中补充推理验证结果后重新导出。",
    },
    // 检测到当前对话的模型工作区里存在可用的精度变体时，在编辑器上方浮出的
    // 提示条。会话内按 workdir 记录 dismiss（不做永久关闭），因为触发条件
    // 本身已经足够收敛：真实磁盘状态 + 允许模式 + 已识别到工作区。
    readyNotice: {
      title: "模型可打包 🎉",
      // 中文无单复数变化：One / Many 值相同，保留两条只是为满足 i18n
      // 三语 schema 对齐（英文侧有单复数差异）。
      descriptionOne: "检测到 1 个精度变体，可以打包为 App Builder pack 一键复用。",
      descriptionMany: "检测到 {count} 个精度变体，可以打包为 App Builder pack 一键复用。",
      action: "→ 打包到 App Builder",
      dismiss: "稍后",
    },
  },
  gallery: {
    title: "提交到模型画廊",
    openGallery: "模型画廊",
    tip: "提示：你可以让 AI 生成 MODEL_CARD.md，总结转换流程、参数和结果，供其他用户参考复现。",
    generateModelCard: "生成 MODEL_CARD.md",
    modelCardExists: "工作区中已存在 MODEL_CARD.md",
    submitter: "提交者",
    email: "邮箱",
    modelName: "模型名称",
    category: "类别",
    categoryGenai: "GenAI（LLM/VLM/LVM）",
    categoryNonGenai: "Non-GenAI（CV/ASR/OCR）",
    modelType: "模型类型",
    customer: "客户",
    customCustomer: "自定义客户名称",
    qnnVersion: "QNN 版本",
    quantMethod: "量化方法",
    scenario: "场景 / 用途",
    notebookUrl: "Notebook URL",
    description: "描述",
    filesTitle: "待提交文件",
    selectAll: "全选",
    deselectAll: "取消全选",
    fileRequired: "必需",
    noDlcWarning: "必须选择至少一个 .dlc 文件",
    noFilesFound: "工作区中未发现模型产物，请先完成模型转换。",
    filterPlaceholder: "过滤文件...",
    openFile: "打开文件",
    deleteFile: "删除文件",
    confirmDelete: "确定要从磁盘永久删除“{name}”吗？此操作无法撤销。路径：{path}",
    collapse: "收起",
    expand: "展开",
    generateDesc: "生成",
    generatingDesc: "生成中…",
    generateDescHint: "让云端模型根据工作区文档（优先 MODEL_CARD.md）生成描述",
    selectedSummary: "已选 {count} 个文件，共 {size}",
    missingSubmitter: "请填写提交者姓名",
    missingEmail: "请填写邮箱地址",
    missingModelName: "请填写模型名称",
    scriptsWillZip: "Python 脚本（>{n} 个）将打包为 scripts.zip",
    submit: "提交",
    submitting: "提交中...",
    packagingFile: "正在打包 {name}（{done}/{total}）",
    uploadWaiting: "上传中，等待服务器响应...",
    uploadingModel: "正在上传 {name}",
    minimize: "最小化",
    restore: "恢复对话框",
    cancel: "取消",
    submitSuccess: "提交成功！Upload ID：{id}",
    submitFailed: "提交失败：{reason}",
    checkStatus: "查看审核状态",
    historyTitle: "提交历史（{n}）",
    required: "此字段为必填项",
    invalidEmail: "请输入有效的邮箱地址",
    scanning: "正在扫描工作区...",
    noWorkspace: "没有可用的模型工作区，请先完成模型转换。",
  },
};

export default modelBuilder;
