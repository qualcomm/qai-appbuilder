// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Model-conversion environment API client.
 *
 *   GET    /api/system/builder-env          — is the environment usable + the install job, if any
 *   POST   /api/system/builder-env/install  — start the (background) install, 202
 *   DELETE /api/system/builder-env/install  — cancel the running install
 *
 * The environment is the x64 Python 3.10 converter venv + the QAIRT SDK. The user
 * starts the install from the Downloads page; the backend runs it as a child process
 * and this client only polls its progress, so nothing else in the app is blocked.
 */

import { apiJson, type ApiRequestOptions } from "./http";

/** One reason the environment is not ready (`code` is a stable identifier). */
export interface BuilderEnvProblem {
  code: string;
  path: string;
  key: string;
}

export type BuilderEnvJobState = "running" | "succeeded" | "failed" | "cancelled";

export interface BuilderEnvJob {
  job_id: string;
  state: BuilderEnvJobState;
  /** 0–100, never decreases within a job. */
  overall: number;
  /** Step id: python310 | venv | deps | sdk_download | sdk_extract | config | verify. */
  step: string;
  step_state: string;
  message: string;
  /** Unit of done/total for the step in flight: "bytes" | "percent" | "phase" | "". */
  unit: string;
  done: number;
  total: number | null;
  /** Bytes per second (0 when unknown). */
  speed: number;
  eta: number | null;
  log_tail: string[];
  error_code: string;
  error_message: string;
  failed_step: string;
  started_at: number;
  finished_at: number | null;
}

export interface BuilderEnvStatus {
  ready: boolean;
  /** Coarse parts that are not usable: "config" | "sdk" | "venv". */
  missing: string[];
  problems: BuilderEnvProblem[];
  sdk_root: string;
  venv: string;
  config_file: string;
  /** QAIRT version this app installs (scripts/qairt_release.json); "" when unknown. */
  sdk_version?: string;
  /** QAIRT version the current qairt_env.json names; "" when none. */
  installed_sdk_version?: string;
  job: BuilderEnvJob | null;
  /** False when the installer script is not part of this build. */
  installable: boolean;
}

export type BuilderEnvMirror = "auto" | "official" | "cn";

export interface BuilderEnvInstallRequest {
  mirror?: BuilderEnvMirror;
  /** Empty = none. Must start with http://, https:// or socks5://. */
  proxy?: string;
}

export interface BuilderEnvInstallStarted {
  job_id: string;
  status: "queued";
}

export function fetchBuilderEnv(opts?: ApiRequestOptions): Promise<BuilderEnvStatus> {
  return apiJson<BuilderEnvStatus>("GET", "/api/system/builder-env", undefined, opts);
}

export function startBuilderEnvInstall(
  body: BuilderEnvInstallRequest = {},
  opts?: ApiRequestOptions,
): Promise<BuilderEnvInstallStarted> {
  return apiJson<BuilderEnvInstallStarted>("POST", "/api/system/builder-env/install", body, opts);
}

export function cancelBuilderEnvInstall(
  opts?: ApiRequestOptions,
): Promise<{ cancelled: boolean }> {
  return apiJson<{ cancelled: boolean }>(
    "DELETE",
    "/api/system/builder-env/install",
    undefined,
    opts,
  );
}
