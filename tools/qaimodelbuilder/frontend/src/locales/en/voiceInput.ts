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

const voiceInput = {
  cancel: "Cancel",
  cancelTitle: "Click to cancel transcription",
  coldBadge: "○ Cold",
  coldTooltip: "First use will load model (~10-15s)",
  disabled:
    "Voice input is disabled. Click the voice engine selector in the toolbar to enable it.",
  dismiss: "Dismiss",
  empty: "No audio captured. Please check your microphone and try again.",
  encodeFailed: "Failed to encode audio",
  engine: {
    whisper: "Whisper (Multilingual)",
    zipformer: "Zipformer (Chinese)",
  },
  engineLabel: "Engine",
  engineMenuHint: "ⓘ Tip: first use loads the model (~10-15s), then stays ready; auto-releases after 10 min idle",
  engineMenuTitle: "Voice input engine",
  errorTitle: "Voice input failed",
  inferError: "Speech model \"{model}\" failed: {detail}. Try switching to {other} from the engine menu, or re-download the model in AppBuilder.",
  inferenceFailed: "Voice transcription failed",
  listening: "Listening…",
  loadingBadge: "◐ Loading",
  loadingTooltip: "Model is loading in background",
  noTranscript: "No speech recognized. Please try again with clearer audio.",
  permissionDenied: "Microphone permission denied. Please allow microphone access in your browser settings.",
  phase: {
    preparing: "Loading model…",
    processing: "Processing…",
    queued: "Queued…",
    running: "Transcribing…",
    uploading: "Uploading…",
  },
  popoverAria: "Voice input controls",
  preloadingHint: "Loading model in background — keep speaking",
  processingTitle: "Transcribing… use ✕ to cancel",
  retry: "Retry",
  startAria: "Start voice input",
  startFailed: "Failed to start microphone",
  startTitle: "Click to start voice input",
  stopAndTranscribe: "Stop & Transcribe",
  stopAria: "Stop voice input",
  stopTitle: "Click to stop and transcribe",
  transcribing: "Transcribing voice input…",
  unsupported: "Voice input is only supported on desktop Chrome or Edge.",
  unsupportedTitle: "Voice input requires desktop Chrome or Edge",
  uploadFailed: "Failed to upload audio",
  warmBadge: "● Ready",
  warmTooltip: "Model is loaded · transcription will be fast",
  weightsMissing: "Voice model weights are not installed. Please open AppBuilder and download {model} first.",
};

export default voiceInput;
