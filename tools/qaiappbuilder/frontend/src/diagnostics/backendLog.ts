// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Support helpers for the sidebar user menu: the backend-log excerpt (copy /
 * export, so a user can attach it to a problem report) and "contact support".
 *
 * The backend reads ``data/logs/backend.log`` (+ ``desktop-backend.log``) and
 * masks credentials before returning it (``GET /api/system/backend-log``).
 */
import { apiBlob, apiJson } from "@/api/http";

export const BACKEND_LOG_ENDPOINT = "/api/system/backend-log";
export const CONTACT_SUPPORT_ENDPOINT = "/api/system/contact-support";

/** Lines for "copy" (small enough to paste into an e-mail) and "export" (a file). */
export const COPY_LOG_LINES = 2000;
export const EXPORT_LOG_LINES = 20000;

export async function fetchBackendLogText(lines: number): Promise<string> {
  const blob = await apiBlob("GET", BACKEND_LOG_ENDPOINT, { query: { lines } });
  return await blob.text();
}

export interface ContactSupportResult {
  opened: boolean;
  email: string;
}

/** Ask the backend to open the default mail client; ``opened=false`` means show the address instead. */
export async function requestContactSupport(): Promise<ContactSupportResult> {
  return await apiJson<ContactSupportResult>("POST", CONTACT_SUPPORT_ENDPOINT);
}

/**
 * Copy ``text`` to the clipboard. ``navigator.clipboard`` needs a secure
 * context (localhost / https); fall back to a hidden textarea + execCommand.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator.clipboard?.writeText === "function") {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the legacy path
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.body.removeChild(area);
  }
}

/** Save ``text`` as a .txt download named ``qai-backend-log-<timestamp>.txt``. */
export function downloadText(text: string, now: Date = new Date()): string {
  const stamp = now.toISOString().replace(/[:.]/g, "-");
  const name = `qai-backend-log-${stamp}.txt`;
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return name;
}
