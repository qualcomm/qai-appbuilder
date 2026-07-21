// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * API layer entry point.
 *
 * Re-exports the full client surface for use by views / stores /
 * composables. Internal modules can still import directly from
 * `./base`, `./errors`, `./csrf`, `./http`, `./sse`, `./stream` if they
 * want narrower visibility, but most callers should rely on this
 * barrel.
 *
 * Layout (refactor-plan §11.3 step 6):
 *   base.ts    — apiBaseUrl / wsBaseUrl
 *   errors.ts  — ApiError hierarchy + parseApiError
 *   csrf.ts    — qai_csrf cookie / X-QAI-CSRF header helpers
 *   http.ts    — apiJson / apiRaw / apiBlob / apiUpload
 *   sse.ts     — apiSSE (QAI-native event-stream envelope)
 *   stream.ts  — apiStream (OpenAI Compat data: ... [DONE] format)
 */

// Base URL helpers (PR-050 surface — kept identical for callers).
export { apiBaseUrl, wsBaseUrl } from "./base";

// Error model.
export {
  ApiError,
  ValidationApiError,
  UnauthorizedApiError,
  ForbiddenApiError,
  NotFoundApiError,
  ConflictApiError,
  PreconditionFailedApiError,
  RateLimitedApiError,
  DomainApiError,
  InfrastructureApiError,
  UnknownApiError,
  parseApiError,
  NO_STATUS,
} from "./errors";

// CSRF helpers (re-exported so downstream auth-aware code can read them).
export {
  QAI_CSRF_COOKIE,
  QAI_CSRF_HEADER,
  readCsrfCookie,
  attachCsrfHeader,
  methodNeedsCsrf,
} from "./csrf";

// HTTP variants.
export {
  apiJson,
  apiRaw,
  apiBlob,
  apiUpload,
  buildApiUrl,
  type ApiMethod,
  type ApiRequestOptions,
  type QueryValue,
} from "./http";

// SSE.
export {
  apiSSE,
  type SseHandler,
  type SseOptions,
  type SseEventName,
} from "./sse";

// OpenAI Compat stream.
export { apiStream, type StreamHandler, type StreamOptions } from "./stream";

// WebSocket-first streaming with SSE fallback.
export { apiWsStream, type WsStreamOptions } from "./wsStream";

// Domain API clients (v1 parity).
export { fetchCloudModels, fetchCloudProviders } from "./cloudModels";
export { fetchChatTools, type ChatToolDescriptor, type ListChatToolsResponse } from "./chatTools";
export {
  fetchCcConfig,
  updateCcConfig,
  fetchOcConfig,
  updateOcConfig,
} from "./codingConfig";
export { fetchCcHealth, fetchOcHealth } from "./aiCodingHealth";
export {
  fetchCcCredentials,
  saveCcCredentials,
  deleteCcCredential,
  fetchOcCredentials,
  saveOcCredentials,
  deleteOcCredential,
} from "./aiCodingCredentials";
export {
  fetchOcServiceStatus,
  startOcService,
  stopOcService,
  fetchOcServiceLogs,
} from "./ocService";
export {
  fetchServiceStatus,
  probeService,
  startService,
  stopService,
  serviceLogsUrl,
  streamServiceLogs,
  clearServiceLogs,
  fetchServiceModels,
  loadServiceModel,
} from "./serviceControl";
export {
  connectGlobalEvents,
  type GlobalEventHandler,
  type GlobalEventsOptions,
} from "./globalEvents";
export {
  scanBins,
  autoExport,
  importDryRun,
  importCommit,
  importRollback,
  type BinScanResultDTO,
  type BinScanResponseDTO,
  type AutoExportRequestDTO,
  type AutoExportResponseDTO,
  type ImportPlanItemDTO,
  type ImportPlanResponseDTO,
  type ImportCommitResponseDTO,
  type ImportRollbackResponseDTO,
} from "./appBuilderImport";
