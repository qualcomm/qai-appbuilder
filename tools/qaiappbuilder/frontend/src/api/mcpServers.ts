// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * MCP (Model Context Protocol) servers API client.
 *
 * Covers the chat-context MCP management + marketplace endpoints:
 *   GET    /api/chat/mcp/servers                 — list servers + live status
 *   POST   /api/chat/mcp/servers                 — add / replace a server
 *   PATCH  /api/chat/mcp/servers/{name}          — flip per-server enabled switch
 *   DELETE /api/chat/mcp/servers/{name}          — remove a server
 *   POST   /api/chat/mcp/servers/{name}/test     — re-connect + re-discover
 *   GET    /api/chat/mcp/catalog                 — curated marketplace source
 *   POST   /api/chat/mcp/catalog/{id}/install    — install a curated entry
 *
 * Types are declared locally (not imported from the auto-generated
 * `@/types/api`) so this client is self-contained regardless of the OpenAPI
 * regen cadence. The shapes mirror the backend DTOs in
 * `interfaces/http/routes/chat/_mcp.py`.
 *
 * Credential header VALUES are sent on POST but NEVER echoed back — the
 * server masks them with the `"__secret__"` sentinel in every response.
 */

import { apiJson, type ApiRequestOptions } from "./http";

export type McpTransport = "stdio" | "sse" | "http";

/** Request body for adding / replacing a server. */
export interface McpServerConfigInput {
  name: string;
  transport: McpTransport;
  command?: string | null;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string | null;
  url?: string | null;
  headers?: Record<string, string>;
  timeout_s?: number;
}

/** One server's config + live connection status (response shape). */
export interface McpServerStatus {
  name: string;
  transport: McpTransport;
  command?: string | null;
  args: string[];
  env: Record<string, string>;
  cwd?: string | null;
  url?: string | null;
  headers: Record<string, string>;
  timeout_s: number;
  connected: boolean;
  tool_count: number;
  tool_names: string[];
  /** Number of resources discovered on the last successful connect. */
  resource_count?: number;
  /** Number of prompts discovered on the last successful connect. */
  prompt_count?: number;
  /** Per-server master switch (independent of the global gate). */
  enabled?: boolean;
  error: string;
}

/** One curated marketplace catalog entry. */
export interface McpCatalogEntry {
  id: string;
  name: string;
  description: string;
  source: string;
  install_type: string;
  command: string;
  args_template: string[];
  requires_args: string[];
  env_schema: string[];
  homepage: string;
  // ── Phase-2 additive fields (all optional/defaulted; a phase-1 curated
  // entry omits them). See interfaces/http/routes/chat/_mcp.py. ──────────────
  /** Transport kind ("stdio" | "sse" | "http"). Defaults to stdio when absent. */
  transport?: string;
  /** Remote endpoint URL (sse/http registry entries). */
  url?: string;
  /** Env var names the user MUST supply (stdio keyed entries). */
  env_required?: string[];
  /** All env var names the entry understands (superset of env_required). */
  headers_schema?: string[];
  /** Header names the user MUST supply (remote sse/http entries). */
  headers_required?: string[];
  /**
   * Subset of env/header names whose values are secret — render these as
   * <input type="password"> and send via env_values / header_values.
   */
  secret_fields?: string[];
  /** Human-readable labels for header fields, keyed by raw header name. */
  headers_labels?: Record<string, string>;
  /** OAuth URL to open in browser for token acquisition (e.g. Orbit). */
  oauth_url?: string;
  /**
   * OAuth 2.1 + PKCE metadata base URL — when set the install dialog triggers
   * the full browser auth flow (backend discovers endpoints, exchanges code,
   * and auto-fills the Authorization field).
   */
  oauth_metadata_url?: string;
}

export interface McpCatalogResponse {
  entries: McpCatalogEntry[];
  /** Catalog source ids present (["curated"] or ["curated","registry"]). */
  sources: string[];
  /**
   * Non-empty when the dynamic official-registry source is degraded (unreachable
   * / errored). "" when healthy or gated off. Phase-2 additive.
   */
  registry_error?: string;
}

export interface McpServerListResponse {
  servers: McpServerStatus[];
  /** Whether the master MCP execution gate (chat_mcp_enabled) is on. */
  enabled: boolean;
}

/** One page of registry entries (GET /api/chat/mcp/catalog/browse). */
export interface McpCatalogBrowseResponse {
  entries: McpCatalogEntry[];
  /** Opaque cursor for the NEXT page; null / absent means the last page. */
  next_cursor?: string | null;
  /** Non-empty when the browse could not reach the registry (soft banner). */
  registry_error?: string;
}

/** List all configured MCP servers with their live connection status. */
export async function listMcpServers(
  opts?: ApiRequestOptions,
): Promise<McpServerListResponse> {
  return apiJson<McpServerListResponse>(
    "GET",
    "/api/chat/mcp/servers",
    undefined,
    opts,
  );
}

/** Add (or replace, by name) a server; the backend connects + discovers. */
export async function addMcpServer(
  config: McpServerConfigInput,
  opts?: ApiRequestOptions,
): Promise<McpServerStatus> {
  return apiJson<McpServerStatus, McpServerConfigInput>(
    "POST",
    "/api/chat/mcp/servers",
    config,
    opts,
  );
}

/** Remove a server + drop its tools. */
export async function removeMcpServer(
  name: string,
  opts?: ApiRequestOptions,
): Promise<void> {
  await apiJson<void>(
    "DELETE",
    `/api/chat/mcp/servers/${encodeURIComponent(name)}`,
    undefined,
    opts,
  );
}

/** Re-connect + re-discover an already-registered server (Test connection). */
export async function testMcpServer(
  name: string,
  opts?: ApiRequestOptions,
): Promise<McpServerStatus> {
  return apiJson<McpServerStatus>(
    "POST",
    `/api/chat/mcp/servers/${encodeURIComponent(name)}/test`,
    undefined,
    opts,
  );
}

/** Flip one server's per-server ``enabled`` switch (on→connect, off→drop). */
export async function setServerEnabled(
  name: string,
  enabled: boolean,
  opts?: ApiRequestOptions,
): Promise<McpServerStatus> {
  return apiJson<McpServerStatus, { enabled: boolean }>(
    "PATCH",
    `/api/chat/mcp/servers/${encodeURIComponent(name)}`,
    { enabled },
    opts,
  );
}

/** Fetch the curated marketplace catalog. */
export async function listCatalog(
  opts?: ApiRequestOptions,
): Promise<McpCatalogResponse> {
  return apiJson<McpCatalogResponse>(
    "GET",
    "/api/chat/mcp/catalog",
    undefined,
    opts,
  );
}

/**
 * Force a dynamic re-fetch of the catalog, bypassing the server-side TTL cache.
 * Returns the SAME McpCatalogResponse shape as `listCatalog`.
 */
export async function refreshCatalog(
  opts?: ApiRequestOptions,
): Promise<McpCatalogResponse> {
  return apiJson<McpCatalogResponse>(
    "POST",
    "/api/chat/mcp/catalog/refresh",
    undefined,
    opts,
  );
}

/** Request body for installing a catalog entry. */
export interface McpInstallBody {
  name?: string;
  arg_values?: Record<string, string>;
  /** Values for stdio-keyed entries (entry.env_required / env_schema). */
  env_values?: Record<string, string>;
  /** Values for remote entries (entry.headers_required / headers_schema). */
  header_values?: Record<string, string>;
  /**
   * Source id ("curated" | "registry") disambiguating a shared entry id — when
   * a registry server slug collides with a curated id (e.g. both "git"), this
   * pins the install to the source the user actually clicked. Omitted → the
   * backend falls back to curated-first.
   */
  source?: string;
}

/** Install one curated catalog entry (materialise + connect). */
export async function installFromCatalog(
  entryId: string,
  body: McpInstallBody,
  opts?: ApiRequestOptions,
): Promise<McpServerStatus> {
  return apiJson<McpServerStatus, McpInstallBody>(
    "POST",
    `/api/chat/mcp/catalog/${encodeURIComponent(entryId)}/install`,
    body,
    opts,
  );
}

/**
 * Flip the GLOBAL master switch (on → connect every enabled server, off →
 * disconnect them all). Returns the updated server list + the new `enabled`.
 */
export async function setGlobalEnabled(
  enabled: boolean,
  opts?: ApiRequestOptions,
): Promise<McpServerListResponse> {
  return apiJson<McpServerListResponse, { enabled: boolean }>(
    "PATCH",
    "/api/chat/mcp/enabled",
    { enabled },
    opts,
  );
}

/** Query params for browsing the dynamic registry source (search + paginate). */
export interface McpBrowseParams {
  /** Server-side `name` substring filter (the registry only searches name). */
  search?: string;
  /** Opaque cursor from a prior page's `next_cursor` (omit for the first page). */
  cursor?: string;
  /** Page size (server clamps to 1..100; UI default 30). */
  limit?: number;
  /** Source id to browse: omit / "registry" for built-in, custom uuid for user-added. */
  sourceId?: string;
}

/**
 * Browse ONE page of a registry source (search + cursor pagination).
 * User-driven network fetch; degrades to an empty page + `registry_error` on
 * failure (never throws for a registry outage — the backend returns 200).
 */
export async function browseRegistry(
  params: McpBrowseParams = {},
  opts?: ApiRequestOptions,
): Promise<McpCatalogBrowseResponse> {
  const qs = new URLSearchParams();
  if (params.search) qs.set("search", params.search);
  if (params.cursor) qs.set("cursor", params.cursor);
  if (params.limit != null) qs.set("limit", String(params.limit));
  if (params.sourceId) qs.set("source_id", params.sourceId);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiJson<McpCatalogBrowseResponse>(
    "GET",
    `/api/chat/mcp/catalog/browse${suffix}`,
    undefined,
    opts,
  );
}

// ── Registry source management ────────────────────────────────────────────────

export interface McpRegistrySource {
  id: string;
  name: string;
  url: string;
  builtin: boolean;
}

/** List all registry sources (built-in + user-added custom ones). */
export async function listRegistrySources(
  opts?: ApiRequestOptions,
): Promise<McpRegistrySource[]> {
  return apiJson<McpRegistrySource[]>("GET", "/api/chat/mcp/catalog/sources", undefined, opts);
}

/** Add a custom registry source. Returns the created source. */
export async function addRegistrySource(
  url: string,
  name?: string,
  token?: string,
  cookie?: string,
  opts?: ApiRequestOptions,
): Promise<McpRegistrySource> {
  return apiJson<McpRegistrySource, { url: string; name: string; token: string; cookie: string }>(
    "POST",
    "/api/chat/mcp/catalog/sources",
    { url, name: name ?? "", token: token ?? "", cookie: cookie ?? "" },
    opts,
  );
}

/** Delete a custom registry source by id. Built-in sources will return 404. */
export async function removeRegistrySource(
  sourceId: string,
  opts?: ApiRequestOptions,
): Promise<void> {
  await apiJson<void>(
    "DELETE",
    `/api/chat/mcp/catalog/sources/${encodeURIComponent(sourceId)}`,
    undefined,
    opts,
  );
}

// ── OAuth 2.1 + PKCE flow ────────────────────────────────────────────────────

export interface McpOAuthStartResponse {
  auth_url: string;
  session_id: string;
}

export interface McpOAuthTokenResponse {
  status: "pending" | "done" | "error";
  access_token?: string;
  error?: string;
}

/**
 * The theme keys the OAuth callback page renders, in one place.
 *
 * Must stay in step with `_THEME_KEYS` in
 * `interfaces/http/routes/chat/_mcp.py`: a key the backend reads but this does
 * not send silently renders as the hardcoded fallback, which is how the icon
 * gradient's second stop stopped following the theme. Two copies of this list
 * (one per start endpoint) is what let them diverge, so both share this one.
 */
const _OAUTH_PAGE_THEME_VARS = [
  "bg-primary",
  "bg-secondary",
  "text-primary",
  "text-secondary",
  "border",
  "accent",
  "accent-hover",
] as const;

/** Snapshot the current app palette for the themed OAuth callback page. */
function _pageTheme(): Record<string, string> {
  const style = getComputedStyle(document.documentElement);
  const theme: Record<string, string> = {};
  for (const key of _OAUTH_PAGE_THEME_VARS) {
    const value = style.getPropertyValue(`--${key}`).trim();
    // Skip empties: the backend falls back to its own default, which beats
    // sending an empty string that would render as an invalid CSS value.
    if (value) theme[key] = value;
  }
  return theme;
}

/** Start the OAuth PKCE flow; returns the browser auth URL + a session id. */
export async function startOAuth(
  entryId: string,
  opts?: ApiRequestOptions,
): Promise<McpOAuthStartResponse> {
  const theme = _pageTheme();
  return apiJson<McpOAuthStartResponse, { theme: Record<string, string> }>(
    "POST",
    `/api/chat/mcp/catalog/${encodeURIComponent(entryId)}/oauth/start`,
    { theme },
    opts,
  );
}

/** Poll for the OAuth token (status: "pending" | "done" | "error"). */
export async function pollOAuthToken(
  entryId: string,
  sessionId: string,
  opts?: ApiRequestOptions,
): Promise<McpOAuthTokenResponse> {
  return apiJson<McpOAuthTokenResponse>(
    "GET",
    `/api/chat/mcp/catalog/${encodeURIComponent(entryId)}/oauth/token?session_id=${encodeURIComponent(sessionId)}`,
    undefined,
    opts,
  );
}

/** Start the OAuth PKCE flow for a registry *source* (e.g. QGenie MCPHub). */
export async function startSourceOAuth(
  sourceId: string,
  opts?: ApiRequestOptions,
): Promise<McpOAuthStartResponse> {
  const theme = _pageTheme();
  return apiJson<McpOAuthStartResponse, { theme: Record<string, string> }>(
    "POST",
    `/api/chat/mcp/catalog/sources/${encodeURIComponent(sourceId)}/oauth/start`,
    { theme },
    opts,
  );
}

/** Poll for the OAuth token of a registry *source*. */
export async function pollSourceOAuthToken(
  sourceId: string,
  sessionId: string,
  opts?: ApiRequestOptions,
): Promise<McpOAuthTokenResponse> {
  return apiJson<McpOAuthTokenResponse>(
    "GET",
    `/api/chat/mcp/catalog/sources/${encodeURIComponent(sourceId)}/oauth/token?session_id=${encodeURIComponent(sessionId)}`,
    undefined,
    opts,
  );
}
