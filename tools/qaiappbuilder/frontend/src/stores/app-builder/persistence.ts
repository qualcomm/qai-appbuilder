// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * App Builder last-used-model persistence (localStorage).
 *
 * Extracted verbatim from `stores/appBuilder.ts` (ARCH-1 cohesion split).
 * Pure side-effecting helpers guarded against private-mode / SSR where
 * `localStorage` may be unavailable. Reused by the store's init fallback +
 * `selectModel`.
 */
import { LAST_MODEL_KEY } from "@/stores/_appBuilderHelpers";

export function readLastModel(): string | null {
  try {
    return globalThis.localStorage?.getItem(LAST_MODEL_KEY) ?? null;
  } catch {
    return null;
  }
}

export function writeLastModel(modelId: string): void {
  try {
    globalThis.localStorage?.setItem(LAST_MODEL_KEY, modelId);
  } catch {
    // ignore (private mode / SSR)
  }
}
