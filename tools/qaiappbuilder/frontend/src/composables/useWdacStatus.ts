// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useWdacStatus` — detect whether this Windows host enforces code integrity
 * (WDAC user-mode enforcement or Smart App Control) in a way that BLOCKS the
 * unsigned QAIRT converter native extension (e.g. `libPyIrGraph310.pyd`).
 *
 * Why: those SDK `.pyd`/`.dll` files are unsigned third-party binaries under
 * the write-protected `C:\Qualcomm` tree, so they cannot be re-signed. When
 * the OS enforces code integrity the kernel refuses to load them and **Model
 * Builder (model conversion) cannot work**. This composable lets the chat
 * welcome screen surface a heads-up + how-to-disable guidance.
 *
 * Purely additive + best-effort: it reads a single existing system endpoint
 * (`GET /api/system/wdac`, cached server-side) and defaults to "not enabled"
 * on any error — the warning must never cry wolf. Result is cached on a
 * module-level singleton so multiple mounts share one fetch.
 */
import { ref, type Ref } from "vue";
import { apiJson } from "@/api";

interface WdacStatusResponse {
  enabled: boolean;
  umci: number;
  sac: number;
}

// ─── Module-level singleton (shared across all callers) ─────────────────────
const enabled = ref(false);
/** True once a fetch has completed at least once (success or failure). */
const checked = ref(false);
/** User-dismissed the banner this session (not persisted — resets on reload). */
const dismissed = ref(false);
let inflight: Promise<void> | null = null;

async function ensureFetched(): Promise<void> {
  if (checked.value) return;
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const res = await apiJson<WdacStatusResponse>("GET", "/api/system/wdac");
      enabled.value = res.enabled === true;
    } catch {
      // Fail-open: never warn on an unconfirmed host.
      enabled.value = false;
    } finally {
      checked.value = true;
      inflight = null;
    }
  })();
  return inflight;
}

export interface UseWdacStatus {
  /** True when code-integrity enforcement blocks the unsigned converter. */
  enabled: Ref<boolean>;
  /** True after the first fetch settles. */
  checked: Ref<boolean>;
  /** True once the user dismisses the banner this session. */
  dismissed: Ref<boolean>;
  /** Fetch once (no-op if already fetched). Call on mount. */
  ensureFetched: () => Promise<void>;
  /** Hide the banner for the rest of this session. */
  dismiss: () => void;
}

export function useWdacStatus(): UseWdacStatus {
  return {
    enabled,
    checked,
    dismissed,
    ensureFetched,
    dismiss: () => {
      dismissed.value = true;
    },
  };
}
