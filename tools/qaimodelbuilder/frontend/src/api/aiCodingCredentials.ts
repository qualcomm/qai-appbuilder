// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * AI Coding credentials API client.
 *
 * Covers (dual-mounted on CC and OC prefixes):
 *   GET    /api/{cc,oc}/credentials              — list credential status
 *   POST   /api/{cc,oc}/credentials              — save/delete credentials
 *   DELETE /api/{cc,oc}/credentials/{var_name}   — delete a single credential
 *
 * Sensitive credential material (API keys / tokens / passwords) is
 * stored exclusively in the platform SecretStore (OS keyring + Fernet
 * fallback) per v2.7 §3.3 — never in the config document. Value
 * semantics on save: empty string → delete; "****" → masked, skipped;
 * any other value → stored + injected into the process env.
 */

import { apiJson, type ApiRequestOptions } from "./http";
import type {
  CredentialsListResponse,
  SaveCredentialsRequest,
  SaveCredentialsResponse,
  DeleteCredentialResponse,
} from "@/types/aiCoding";

// ---------------------------------------------------------------------------
// Claude Code (CC)
// ---------------------------------------------------------------------------

export async function fetchCcCredentials(
  opts?: ApiRequestOptions,
): Promise<CredentialsListResponse> {
  return apiJson<CredentialsListResponse>(
    "GET",
    "/api/cc/credentials",
    undefined,
    opts,
  );
}

export async function saveCcCredentials(
  credentials: Record<string, string>,
  opts?: ApiRequestOptions,
): Promise<SaveCredentialsResponse> {
  const body: SaveCredentialsRequest = { credentials };
  return apiJson<SaveCredentialsResponse, SaveCredentialsRequest>(
    "POST",
    "/api/cc/credentials",
    body,
    opts,
  );
}

export async function deleteCcCredential(
  varName: string,
  opts?: ApiRequestOptions,
): Promise<DeleteCredentialResponse> {
  return apiJson<DeleteCredentialResponse>(
    "DELETE",
    `/api/cc/credentials/${encodeURIComponent(varName)}`,
    undefined,
    opts,
  );
}

// ---------------------------------------------------------------------------
// OpenCode (OC)
// ---------------------------------------------------------------------------

export async function fetchOcCredentials(
  opts?: ApiRequestOptions,
): Promise<CredentialsListResponse> {
  return apiJson<CredentialsListResponse>(
    "GET",
    "/api/oc/credentials",
    undefined,
    opts,
  );
}

export async function saveOcCredentials(
  credentials: Record<string, string>,
  opts?: ApiRequestOptions,
): Promise<SaveCredentialsResponse> {
  const body: SaveCredentialsRequest = { credentials };
  return apiJson<SaveCredentialsResponse, SaveCredentialsRequest>(
    "POST",
    "/api/oc/credentials",
    body,
    opts,
  );
}

export async function deleteOcCredential(
  varName: string,
  opts?: ApiRequestOptions,
): Promise<DeleteCredentialResponse> {
  return apiJson<DeleteCredentialResponse>(
    "DELETE",
    `/api/oc/credentials/${encodeURIComponent(varName)}`,
    undefined,
    opts,
  );
}
