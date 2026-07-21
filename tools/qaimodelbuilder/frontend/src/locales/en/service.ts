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

const service = {
  adapter: "LoRA Adapter",
  allText: "All text mode",
  autoScroll: "Auto-scroll",
  clearLogs: "Clear logs",
  close: "Close",
  commandPreview: "Command Preview",
  config: "Config",
  configRequiresInstall: "Install GenieAPIService before configuring",
  copyFailed: "Copy failed",
  editInConfig: "Edit in Service Config",
  enableThinking: "Enable thinking",
  host: "Host",
  launchParams: "Launch Parameters",
  launchParamsHint: "These reflect the GenieAPIService launch config (service_launch). Edit them in the Service Config dialog.",
  loadModel: "Load model on start",
  localMode: "Local",
  logLevel: "Log Level",
  logLevelError: "Error",
  logLevelWarning: "Warning",
  logLevelInfo: "Info",
  logLevelDebug: "Debug",
  logLevelVerbose: "Verbose",
  logs: "Service Logs",
  logsCopied: "Copied {n} log lines to clipboard",
  loraAlpha: "LoRA Alpha",
  model: "Model",
  modelAuto: "Use configured default",
  modelsRoot: "Models Root Path",
  noLogs: "No logs yet...",
  noModels: "No models found",
  noModelsAvailable: "No models available in the models directory, please check Models Root Path",
  off: "Off",
  on: "On",
  paramsSaved: "Parameters saved",
  pid: "PID",
  port: "Port",
  portDefault: "Default (service_launch)",
  promptDebug: "Prompt debug",
  reboot: "Reboot",
  rebooting: "Rebooting...",
  remoteMode: "Remote",
  running: "Running",
  saveFailed: "Save failed",
  saveParams: "Save Parameters",
  selectModel: "Select Model",
  selectModelFirst: "Please select a model first",
  start: "Start",
  startFailed: "Failed to start service",
  portInUse:
    "Port {port} is already in use — a service may already be running. Stop the existing service or choose a different port, then try again.",
  startSuccess: "Service started successfully",
  starting: "Starting...",
  stop: "Stop",
  stopFailed: "Failed to stop service",
  stopSuccess: "Service stopped",
  stopped: "Stopped",
  stopping: "Stopping...",
  uptime: "Uptime",
  connection: "Connection",
  localPrefix: "This machine · ",
  remotePrefix: "Remote · ",
  editArrow: "Edit ▾",
  closeArrow: "Close ▴",
  connectionQuestion: "Where is the model service running?",
  thisMachine: "This machine (localhost)",
  remoteMachine: "Remote machine",
  ipAddress: "IP Address",
  test: "Test",
  reachable: "Reachable",
  unreachable: "Unreachable",
  save: "Save",
  remoteModeStartStopWarn:
    "Remote mode: Start/Stop is disabled — the service runs on the remote host.",
  remoteModeStartHint:
    "Remote mode: start the service on the remote host.",
  warnPathChineseSpaces:
    "Install path contains Chinese characters or spaces, which may break model loading",
  warnPathQnnDesc1:
    "The GenieAPIService QNN backend cannot handle paths containing Chinese or spaces during initialization.",
  warnPathMigrate:
    "Please migrate GenieAPIService and model files to a {bold} directory, e.g.:",
  pureEnglishNoSpaces: "pure-English, space-free",
  orWord: "or",
  modifyInstallPath: "🔧 Modify install path →",
  redownloadInstall: "📥 Re-download and install →",
  serviceNotFound: "GenieAPIService not found",
  downloadArrow: "Download →",
  noModelsAvailablePrefix: "No models available — ",
  goDownloadArrow: "go download →",
  geniesvcNotFoundTitle: "GenieAPIService not installed",
  geniesvcNotFoundBody:
    "The GenieAPIService binary was not found. Download it from the Download Center or set its path in Service Config.",
  gotoDownloadGeniesvc: "Download GenieAPIService",
  gotoDownloadCenterModels: "Download models in the Download Center",
  noUsableModelsTitle: "No usable models found",
  noUsableModelsBody:
    "No usable models (QNN / GGUF / MNN) were found in the models directory.",
  loadingDots: "Loading...",
  streamingLive: "Streaming live",
  linesUnit: "lines",
  copyLogs: "Copy logs",
  collapseLogArea: "Collapse log area",
  expandLogArea: "Expand log area",
  scrollTop: "Scroll to top",
  scrollBottomLog: "Scroll to bottom",
  refreshStatus: "Refresh Status",
  setPathArrow: "🔧 Set path →",
  setGeniesvcPath: "🔧 Set GenieAPIService install path",
  setModelsRootRescan: "🔧 Set Models Root Path, then click 🔍 Rescan",
  // Download / install in-flight hints (V1 isAnyModelDownloading / isAnyModelInstalling)
  modelDownloadingHint: "A model is being prepared.",
  modelDownloadingTitle: "Model download in progress",
  modelDownloadingBody:
    "A model is being downloaded. Once it finishes it will appear in the list below. You can track progress in the Download Center.",
  modelInstallingHint: "📦 Models are installing; the list will refresh when finished.",
  modelInstallingTitle: "Installing model...",
  modelInstallingBody:
    "Extracting and installing models; they will appear in the list below when complete.",
  installingPleaseWait: "Installing, please wait...",
  installingShort: "Installing.",
  downloadingShort: "Downloading.",
  viewProgressArrow: "View progress →",
  viewInDownloadCenter: "View in Download Center",
  downloadingPleaseWait: "Downloading, please wait...",
  // Model count suffix (V1 index.html:2942 "(N model(s))")
  modelsCount: "{n} model(s)",
  versionCheckFailed: "Failed to check versions",
};

export default service;
