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
    conflictDetected: "Conflict detected — choose a policy",
    defaultPrecision: "Default in App Builder",
    disabledReason: {
      exporting: "Generating in progress, please wait…",
      generic: "Cannot generate Pack in current state — check the hints above",
      noBins: "No precision binary found. Convert a model in Model Builder first (produces <model>_<precision>.bin)",
      noDefaultVariant: "Pick a default precision from the checked ones (see the “Default in App Builder” row below)",
      noVariantSelected: "Check at least one precision",
    },
    generate: "Generate App Builder Pack",
    generating: "Generating...",
    import: "Import to App Builder",
    importFailed: "Import failed",
    importSuccess: "Successfully imported to App Builder",
    noBinsHint: "No precision binaries found in output/. Expected format: <model>_<precision>.bin (e.g. model_fp16.bin, model_fp32.bin, model_int8.bin).",
    needsNormalize: {
      title: "Model downloaded but not yet importable",
      body: "An AI Hub model was found in this workspace, but it hasn't been normalized into the App Builder layout yet. Ask the agent to run the Model Hub Step 6.5 normalization (aihub_to_manifest.py) — it creates output/<model>_<precision>.{bin,dlc} + inference_manifest.json so this model can be imported.",
    },
    noCandidates: "No exportable Pack candidates found. Complete Phase 7 in Model Builder to generate one.",
    packGenerated: "Pack generated: {name}",
    policyBump: "Bump version",
    policyCancel: "Cancel if exists",
    policyReplace: "Replace existing",
    ready: "Ready",
    relativeTime: {
      hoursAgo: "{n} h ago",
      justNow: "just now",
      minutesAgo: "{n} min ago",
    },
    repickPrecision: "Re-pick precision",
    rollback: "Rollback",
    rollbackSuccess: "Rolled back to previous version",
    scanBinsTitle: "Variants found in output/",
    scanning: "Scanning the workspace for model variants…",
    sizeMB: "{n} MB",
    title: "Promote to App Builder",
    readyBadgeAria: "Promote target detected — click to review",
    validate: "Validate",
    validationPassed: "Validation passed — ready to import",
    suggestedVersion: "Suggested next version: {v}",
    variantsCount: "{n} variants selected",
    noWorkspace:
      "No model workspace detected in this conversation. Use Model Builder to convert a model first.",
    workspaceFound: "Model workspace found:",
    warn: {
      provenance_failed: "Model accuracy validation not passed — REPORT.md does not contain valid Cosine Similarity values. Suggestion: run inference validation (compare ONNX baseline vs QNN output cosine similarity), write results to REPORT.md (format: Cosine Similarity (ONNX vs FP16): 0.9999), then re-export.",
      provenance_not_found: "Validation record not found (REPORT.md missing Cosine Similarity data). To resolve: add inference validation results to REPORT.md and re-export.",
    },
    // Inline "your model is ready to promote" notice surfaced above the
    // composer whenever `usePromoteReadyDetection` finds scanned-eligible
    // precision variants for the active tab's model workspace. Session-
    // scoped dismissal (per workdir) — no permanent-off toggle needed
    // because the detection is self-limiting (real disk state + a real
    // eligible mode + a real workdir).
    readyNotice: {
      title: "Model ready to promote 🎉",
      // Two variants avoid the awkward `variant(s)` parenthesis when
      // count === 1. Component picks between them based on variantCount
      // (see `PromoteReadyNotice.vue`). Chinese variants have no plural
      // morphology so their singular/plural strings are identical — kept
      // as two entries to preserve i18n schema parity across locales.
      descriptionOne:
        "Detected 1 precision variant · package as an App Builder pack for one-click reuse.",
      descriptionMany:
        "Detected {count} precision variants · package as an App Builder pack for one-click reuse.",
      action: "→ Promote to App Builder",
      dismiss: "Later",
    },
  },
};

export default modelBuilder;
