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
  cancel: "取消",
  cancelTitle: "点击取消转写",
  coldBadge: "○ 待加载",
  coldTooltip: "首次使用需加载模型（约 10-15 秒）",
  disabled: "语音输入未启用。请点击工具栏中的语音引擎选择器以启用。",
  dismiss: "关闭",
  empty: "未采集到音频，请检查麦克风后重试。",
  encodeFailed: "音频编码失败",
  engine: {
    whisper: "Whisper（多语言）",
    zipformer: "Zipformer（中文）",
  },
  engineLabel: "引擎",
  engineMenuHint: "ⓘ 提示：首次使用需加载模型（约 10-15 秒），之后一直可用；空闲 10 分钟会自动释放",
  engineMenuTitle: "语音输入引擎",
  errorTitle: "语音输入失败",
  inferError: "语音模型\"{model}\"推理失败：{detail}。可在引擎菜单切到 {other}，或在 AppBuilder 重新下载该模型。",
  inferenceFailed: "语音转写失败",
  listening: "正在聆听…",
  loadingBadge: "◐ 加载中",
  loadingTooltip: "模型正在后台加载中",
  noTranscript: "未识别到语音，请清晰地再说一次。",
  permissionDenied: "麦克风权限被拒绝，请在浏览器设置中允许使用麦克风。",
  phase: {
    preparing: "加载模型…",
    processing: "处理中…",
    queued: "排队中…",
    running: "转写中…",
    uploading: "上传中…",
  },
  popoverAria: "语音输入控件",
  preloadingHint: "模型正在后台加载，请继续讲话",
  processingTitle: "正在转写…使用 ✕ 取消",
  retry: "重试",
  startAria: "开始语音输入",
  startFailed: "麦克风启动失败",
  startTitle: "点击开始语音输入",
  stopAndTranscribe: "停止并转写",
  stopAria: "停止语音输入",
  stopTitle: "点击停止并转写",
  transcribing: "正在转写语音输入…",
  unsupported: "语音输入仅在桌面版 Chrome / Edge 可用。",
  unsupportedTitle: "语音输入仅在桌面版 Chrome / Edge 可用",
  uploadFailed: "音频上传失败",
  warmBadge: "● 已就绪",
  warmTooltip: "模型已加载，转写会很快",
  weightsMissing: "语音模型权重未安装，请先在 AppBuilder 中下载 {model}。",
};

export default voiceInput;
