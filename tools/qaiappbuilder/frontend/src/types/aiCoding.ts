// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * AI Coding type aliases — re-exports from the auto-generated OpenAPI schema.
 *
 * The openapi-typescript generated `api.ts` nests all schemas under
 * `components["schemas"]`. This file lifts the AI Coding–specific types
 * into convenient top-level aliases so API client modules can import them
 * directly:
 *
 *   import type { CredentialsListResponse } from "@/types/aiCoding";
 */

import type { components } from "./api";

// ---------------------------------------------------------------------------
// Credentials (CC + OC)
// ---------------------------------------------------------------------------

/** Per-variable status surfaced via `GET /api/{cc,oc}/credentials`. */
export type CredentialStatusEnvelope =
  components["schemas"]["CredentialStatusEnvelope"];

/** Body of `GET /api/{cc,oc}/credentials`. */
export type CredentialsListResponse =
  components["schemas"]["CredentialsListResponse"];

/** Body of `POST /api/{cc,oc}/credentials` (save). */
export type SaveCredentialsRequest =
  components["schemas"]["SaveCredentialsRequest"];

/** Response of `POST /api/{cc,oc}/credentials`. */
export type SaveCredentialsResponse =
  components["schemas"]["SaveCredentialsResponse"];

/** Response of `DELETE /api/{cc,oc}/credentials/{var_name}`. */
export type DeleteCredentialResponse =
  components["schemas"]["DeleteCredentialResponse"];

// ---------------------------------------------------------------------------
// Coding Config (CC + OC)
// ---------------------------------------------------------------------------

/** Body of `GET /api/{cc,oc}/config`. */
export type CodingConfigResponse =
  components["schemas"]["CodingConfigResponse"];

/** Body of `POST /api/{cc,oc}/config` (save). */
export type SaveCodingConfigRequest =
  components["schemas"]["SaveCodingConfigRequest"];

/** Response of `POST /api/{cc,oc}/config`. */
export type SaveCodingConfigResponse =
  components["schemas"]["SaveCodingConfigResponse"];

// ---------------------------------------------------------------------------
// Health (CC + OC)
// ---------------------------------------------------------------------------

/** Body of `GET /api/{cc,oc}/health`. */
export type InterfacesHttpRoutesAiCodingHealthResponse =
  components["schemas"]["interfaces__http__routes__ai_coding__HealthResponse"];

// ---------------------------------------------------------------------------
// OC Service Control
// ---------------------------------------------------------------------------

/** Response of `GET /api/oc/service/status`. */
export type OcServiceStatusResponse =
  components["schemas"]["OcServiceStatusResponse"];

/** Response of `POST /api/oc/service/start`. */
export type OcServiceStartResponse =
  components["schemas"]["OcServiceStartResponse"];

/** Body of `POST /api/oc/service/stop`. */
export type OcServiceStopRequest =
  components["schemas"]["OcServiceStopRequest"];

/** Response of `POST /api/oc/service/stop`. */
export type OcServiceStopResponse =
  components["schemas"]["OcServiceStopResponse"];

/** Response of `GET /api/oc/service/logs`. */
export type OcServiceLogsResponse =
  components["schemas"]["OcServiceLogsResponse"];
