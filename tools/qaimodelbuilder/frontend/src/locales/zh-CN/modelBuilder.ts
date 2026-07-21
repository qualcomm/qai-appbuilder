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
    needsNormalize: {
      title: "模型已下载，但尚不可导入",
      body: "在该工作区检测到一个 AI Hub 模型，但它还没有被规范化成 App Builder 所需的布局。请让 Agent 执行 Model Hub 的 Step 6.5 规范化（aihub_to_manifest.py）——它会生成 output/<model>_<precision>.{bin,dlc} + inference_manifest.json，之后即可导入该模型。",
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
};

export default modelBuilder;
