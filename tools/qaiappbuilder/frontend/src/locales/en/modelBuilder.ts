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
    aiSearch: {
      button: "AI search for the weights",
      // Weaker, assistive wording for the "scan DID find variants" context: the
      // entry point stays reachable there (a scan can find the wrong thing —
      // e.g. one 293 MB file reported as both FP16 and FP32), but it must not
      // compete with the primary Generate action.
      buttonVerify: "Ask the agent to double-check",
      hint: "Can't find the weight file? Let the agent in this conversation search this workspace and record the result — then click Import again.",
      hintVerify: "Results look wrong? Let the agent in this conversation re-check this workspace's model files against what is actually on disk.",
      cluesTitle: "What the scan saw",
      cluesSubdirs: "Subfolders under output/:",
      cluesRejected: "Files seen but rejected:",
      cluesRejectedRow: "{path} — {size} ({reason})",
      noDiagnostics: "(no diagnostic details were returned by the scan)",
      noPrecisions: "(none detected)",
      // Prompt injected into the active chat session. `{workdir}` is the model
      // workspace and `{outputDir}` / `{manifestPath}` are the same workspace
      // pre-joined with ITS OWN path separator (so a Windows workdir never
      // renders a mixed `C:\a\b/output` path). `{diagnostics}` is the backend's
      // pre-rendered weight-search report (walked dirs / rejected files /
      // suggested next step) so the agent starts from evidence, and
      // `{manifestExample}` is the literal JSON body (a code literal —
      // vue-i18n cannot hold a raw `{` in a message).
      prompt: "The App Builder import could not find a usable NPU weight under `{outputDir}`. Please locate it for me.\n\nBackend weight-search report (evidence — read this first, do not re-derive it):\n---\n{diagnostics}\n---\n\nDo exactly this:\n\n1. Search ONLY inside bounded paths: `{workdir}` and its `output/` subtree, depth 3 at most. NEVER run a recursive scan of `C:\\` or `C:\\WoS_AI` — that walk takes 30+ minutes and will hang.\n2. Look for files with a `.bin` or `.dlc` extension that are at least 1 MiB. Files of 0 bytes (or a few KB) are placeholders, not weights; real context binaries often sit in a subfolder such as `bins/`.\n3. If you find one, write or fix `{manifestPath}` so it contains: {manifestExample} — the `context_binary` path must be relative to the workspace, must stay inside `output/`, must end in `.bin` or `.dlc`, and must be at least 1 MiB. Valid precision tokens: `fp16`, `fp32`, `bf16`, `w8a8` (int8), `w8a16`, `w4a16`, `w4a8` (int4), `w16a16`, or a custom `w<N>a<N>[b<N>]`. Then tell me to click Import again in the App Builder panel.\n4. If there really is no weight, tell me which step is missing (most likely the context-binary generation never completed) and give me the exact next command to run.",
      // Review-toned variant used when the scan DID return variants: the
      // opening "could not find a weight" framing above would contradict the
      // on-screen list. Same hard bounds (bounded walk, never a recursive
      // `C:\` / `C:\WoS_AI` root scan, `.bin`/`.dlc` >= 1 MiB, manifest with
      // `precision` + `context_binary`), plus the two checks the buggy scan
      // failed: one file must not be reported as two precisions, and an
      // auxiliary component must not be reported as a precision at all.
      promptVerify: "The App Builder import panel currently lists these precisions for `{workdir}`: {precisions}. I am not sure that list is right — please verify it against what is actually on disk.\n\nCheck exactly this:\n\n1. Search ONLY inside bounded paths: `{workdir}` and its `output/` subtree, depth 3 at most. NEVER run a recursive scan of `C:\\` or `C:\\WoS_AI` — that walk takes 30+ minutes and will hang.\n2. List every `.bin` or `.dlc` file of at least 1 MiB under `{outputDir}` with its real size. Files of 0 bytes (or a few KB) are placeholders, not weights.\n3. Confirm each listed precision maps to a DISTINCT context binary: two precisions must never point at the same file, and an auxiliary component (a small `flow.bin`, an encoder/decoder part, a `bins/` sidecar) must never be reported as a precision of its own.\n4. Cross-check the list against `{manifestPath}`. If it disagrees with the disk, write or fix that manifest so it contains: {manifestExample} — the `context_binary` path must be relative to the workspace, must stay inside `output/`, must end in `.bin` or `.dlc`, and must be at least 1 MiB. Valid precision tokens: `fp16`, `fp32`, `bf16`, `w8a8` (int8), `w8a16`, `w4a16`, `w4a8` (int4), `w16a16`, or a custom `w<N>a<N>[b<N>]`.\n5. Then tell me which precisions are real, which were misdetected, and whether I can generate the App Builder Pack as-is.",
    },
    needsNormalize: {
      title: "Model downloaded but not yet importable",
      body: "An AI Hub model was found in this workspace, but it hasn't been normalized into the App Builder layout yet. Ask the agent to run the Model Hub Step 6.5 normalization (aihub_to_manifest.py) — it creates output/<model>_<precision>.{'{'}bin,dlc{'}'} + inference_manifest.json so this model can be imported.",
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
  gallery: {
    title: "Submit to Model Gallery",
    openGallery: "Model Gallery",
    tip: "Tip: You can ask the AI to generate a MODEL_CARD.md summarizing the conversion process, parameters, and results for other users to reference.",
    generateModelCard: "Generate MODEL_CARD.md",
    modelCardExists: "MODEL_CARD.md already exists in workspace",
    // Form labels
    submitter: "Submitter",
    email: "Email",
    modelName: "Model Name",
    category: "Category",
    categoryGenai: "GenAI (LLM/VLM/LVM)",
    categoryNonGenai: "Non-GenAI (CV/ASR/OCR)",
    modelType: "Model Type",
    customer: "Customer",
    customCustomer: "Custom customer name",
    qnnVersion: "QNN Version",
    quantMethod: "Quantization Method",
    scenario: "Scenario / Use Case",
    notebookUrl: "Notebook URL",
    description: "Description",
    // File list
    filesTitle: "Files to Submit",
    selectAll: "Select All",
    deselectAll: "Deselect All",
    fileRequired: "required",
    noDlcWarning: "At least one .dlc file must be selected",
    noFilesFound: "No model artifacts found in workspace. Complete a model conversion first.",
    filterPlaceholder: "Filter files...",
    openFile: "Open file",
    deleteFile: "Delete file",
    confirmDelete: "Permanently delete \"{name}\" from disk? This cannot be undone. Path: {path}",
    collapse: "Collapse",
    expand: "Expand",
    generateDesc: "Generate",
    generatingDesc: "Generating…",
    generateDescHint: "Let the cloud model generate a description from workspace docs (MODEL_CARD.md preferred)",
    selectedSummary: "{count} files selected, {size} total",
    missingSubmitter: "Please enter submitter name",
    missingEmail: "Please enter email address",
    missingModelName: "Please enter model name",
    scriptsWillZip: "Python scripts (>{n}) will be packaged as scripts.zip",
    // Actions
    submit: "Submit",
    submitting: "Submitting...",
    packagingFile: "Packaging {name} ({done}/{total})",
    uploadWaiting: "Uploading, waiting for the server...",
    uploadingModel: "Uploading {name}",
    minimize: "Minimize",
    restore: "Restore dialog",
    cancel: "Cancel",
    // Result
    submitSuccess: "Successfully submitted! Upload ID: {id}",
    submitFailed: "Submission failed: {reason}",
    checkStatus: "Check review status",
    historyTitle: "Submission history ({n})",
    // Validation
    required: "This field is required",
    invalidEmail: "Please enter a valid email address",
    scanning: "Scanning workspace...",
    noWorkspace: "No model workspace available. Convert a model first.",
  },
};

export default modelBuilder;
