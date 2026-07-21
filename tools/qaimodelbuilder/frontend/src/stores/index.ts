// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Pinia factory.
 *
 * S5 PR-050: only the Pinia instance lives here. Individual stores
 * sit in sibling files (`ui.ts`, `chatTabs.ts` PR-054, …).
 */
import { createPinia, type Pinia } from "pinia";

export function createAppPinia(): Pinia {
  return createPinia();
}
