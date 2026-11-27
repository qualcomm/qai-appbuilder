// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * User extensions API client (`interfaces/http/routes/extensions.py`).
 *
 *   GET    /api/extensions                       - list + status
 *   POST   /api/extensions/reload                - rescan everything
 *   POST   /api/extensions/{name}/reload|enable|disable
 *   DELETE /api/extensions/{name}?purge_data=     - uninstall
 *   GET    /api/extensions/{name}/settings        - settings card
 *   PUT    /api/extensions/{name}/settings        - save (partial; null clears)
 *   POST   /api/extensions/install/upload|folder  - stage -> capability summary
 *   POST   /api/extensions/install/{token}        - confirm install
 *   DELETE /api/extensions/install/{token}        - discard staged install
 *
 * Secret setting values are write-only: the server only ever says `is_set`.
 */

import { apiJson, apiUpload, type ApiRequestOptions } from "./http";

export type ExtensionState = "loaded" | "failed" | "disabled";

export interface ExtensionStatus {
  name: string;
  version: string;
  state: ExtensionState;
  error: string;
  tools: string[];
  skills: string[];
  warnings: string[];
  /** Prompt fragment counts per condition (`always` / `active`). */
  prompts: Record<string, number>;
  /** App-wide names of the extension's running MCP servers. */
  mcp_servers: string[];
  /** Ships with the app (factory/extensions): can be disabled, not uninstalled. */
  builtin?: boolean;
}

export interface ExtensionListResponse {
  directory: string;
  builtin_directory?: string;
  extensions: ExtensionStatus[];
}

export type SettingType = "text" | "secret" | "bool" | "number" | "select";

export interface ExtensionSetting {
  key: string;
  type: SettingType;
  label: string;
  description: string;
  required: boolean;
  options?: string[];
  min?: number | null;
  max?: number | null;
  default?: unknown;
  /** Current value (absent for secrets). */
  value?: unknown;
  /** Secrets only: whether a value is stored. */
  is_set?: boolean;
}

export interface InstallSummary {
  token: string;
  name: string;
  version: string;
  description: string;
  runs_code: boolean;
  /** JavaScript that runs inside the app's own page (route A). */
  runs_frontend_code?: boolean;
  tools: { name: string; group: string | null }[];
  skills: string[];
  prompts: string[];
  settings: { key: string; type: SettingType; label: string }[];
  mcp_servers: { name: string; transport: string; command: string | null; url: string | null }[];
  installed_version: string | null;
  replaces: boolean;
  files: number;
  bytes: number;
  warnings: string[];
}

const BASE = "/api/extensions";
const enc = encodeURIComponent;

export function listExtensions(opts?: ApiRequestOptions): Promise<ExtensionListResponse> {
  return apiJson<ExtensionListResponse>("GET", BASE, undefined, opts);
}

export function reloadAllExtensions(): Promise<ExtensionListResponse> {
  return apiJson<ExtensionListResponse>("POST", `${BASE}/reload`);
}

export function reloadExtension(name: string): Promise<ExtensionStatus> {
  return apiJson<ExtensionStatus>("POST", `${BASE}/${enc(name)}/reload`);
}

export function setExtensionEnabled(name: string, enabled: boolean): Promise<ExtensionStatus> {
  return apiJson<ExtensionStatus>("POST", `${BASE}/${enc(name)}/${enabled ? "enable" : "disable"}`);
}

export async function uninstallExtension(name: string, purgeData: boolean): Promise<void> {
  await apiJson<void>("DELETE", `${BASE}/${enc(name)}`, undefined, {
    query: { purge_data: purgeData },
  });
}

export async function getExtensionSettings(name: string): Promise<ExtensionSetting[]> {
  const r = await apiJson<{ settings: ExtensionSetting[] }>("GET", `${BASE}/${enc(name)}/settings`);
  return r.settings;
}

export async function saveExtensionSettings(
  name: string,
  values: Record<string, unknown>,
): Promise<ExtensionSetting[]> {
  const r = await apiJson<{ settings: ExtensionSetting[] }, { values: Record<string, unknown> }>(
    "PUT",
    `${BASE}/${enc(name)}/settings`,
    { values },
  );
  return r.settings;
}

export function stageExtensionZip(file: File): Promise<InstallSummary> {
  const form = new FormData();
  form.append("file", file);
  return apiUpload<InstallSummary>(`${BASE}/install/upload`, form);
}

/** A folder chosen with the browser's directory picker (`webkitdirectory`). */
export function stageExtensionFolderFiles(files: readonly File[]): Promise<InstallSummary> {
  const form = new FormData();
  for (const file of files) {
    // `webkitRelativePath` is `<picked folder>/<path inside>`; drop the picked folder's name.
    const rel = file.webkitRelativePath || file.name;
    form.append("files", file, file.name);
    form.append("paths", rel.split("/").slice(1).join("/") || file.name);
  }
  return apiUpload<InstallSummary>(`${BASE}/install/folder-upload`, form);
}

export function stageExtensionFolder(path: string): Promise<InstallSummary> {
  return apiJson<InstallSummary, { path: string }>("POST", `${BASE}/install/folder`, { path });
}

export function confirmExtensionInstall(token: string, replace: boolean): Promise<ExtensionStatus> {
  return apiJson<ExtensionStatus, { replace: boolean }>("POST", `${BASE}/install/${enc(token)}`, {
    replace,
  });
}

export async function discardExtensionInstall(token: string): Promise<void> {
  await apiJson<void>("DELETE", `${BASE}/install/${enc(token)}`);
}
