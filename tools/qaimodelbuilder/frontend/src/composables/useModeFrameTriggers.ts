// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useModeFrameTriggers — pull-based "please open your local panel" signal
 * bus for the mode-frame toolbars (App Builder / Model Builder / GoMaster /
 * Pro / Code).
 *
 * Rationale (Plan §7 decision 5 — C+D combo): the `ModeIntroCard` sits
 * above the composer and exposes action chips that should surface the SAME
 * panels the mode-frame toolbars own. Those panels are local `ref` state
 * on each mode-frame component and rewriting them into shared composables
 * would be a big blast radius. Instead we expose a tiny module-level "bump
 * token" per trigger: the ModeIntroCard emits an action → ChatView calls
 * `requestOpen…()` → the token bumps → the relevant ModeFrame*'s `watch`
 * on the token flips its OWN local `ref` to true.
 *
 * This keeps every mode-frame component's local state exactly as-is; they
 * only need to *react* to the token bump.
 */
import { ref } from "vue";

const _openMyAppsToken = ref(0);
const _openPromoteToken = ref(0);
const _openOptimizeToken = ref(0);
const _openProSettingsToken = ref(0);
const _openProConnectToken = ref(0);
const _openCodePersonaToken = ref(0);
const _openCodeContextToken = ref(0);

/**
 * Shared, module-singleton triggers. All exports are stable references so
 * callers can `watch()` them across mounts without stitching new subscribers.
 */
export function useModeFrameTriggers(): {
  /** Bumps when someone (typically ModeIntroCard) wants App Builder's "My Apps" menu opened. */
  openMyAppsToken: typeof _openMyAppsToken;
  /** Bumps when someone wants the Promote-to-App-Builder popover opened
   *  (whichever mode-frame currently renders it: Model Builder OR App Builder). */
  openPromoteToken: typeof _openPromoteToken;
  /** Bumps when someone wants the GoMaster optimize drawer opened. */
  openOptimizeToken: typeof _openOptimizeToken;
  /** Bumps when someone wants ModeFramePro's Settings dialog opened. */
  openProSettingsToken: typeof _openProSettingsToken;
  /** Bumps when someone wants ModeFramePro's Connect action triggered. */
  openProConnectToken: typeof _openProConnectToken;
  /** Bumps when someone wants ModeFrameCoding's persona picker opened. */
  openCodePersonaToken: typeof _openCodePersonaToken;
  /** Bumps when someone wants ModeFrameCoding's repo/file context input opened. */
  openCodeContextToken: typeof _openCodeContextToken;
  requestOpenMyApps: () => void;
  requestOpenPromote: () => void;
  requestOpenOptimize: () => void;
  requestOpenProSettings: () => void;
  requestOpenProConnect: () => void;
  requestOpenCodePersona: () => void;
  requestOpenCodeContext: () => void;
} {
  return {
    openMyAppsToken: _openMyAppsToken,
    openPromoteToken: _openPromoteToken,
    openOptimizeToken: _openOptimizeToken,
    openProSettingsToken: _openProSettingsToken,
    openProConnectToken: _openProConnectToken,
    openCodePersonaToken: _openCodePersonaToken,
    openCodeContextToken: _openCodeContextToken,
    requestOpenMyApps: () => {
      _openMyAppsToken.value += 1;
    },
    requestOpenPromote: () => {
      _openPromoteToken.value += 1;
    },
    requestOpenOptimize: () => {
      _openOptimizeToken.value += 1;
    },
    requestOpenProSettings: () => {
      _openProSettingsToken.value += 1;
    },
    requestOpenProConnect: () => {
      _openProConnectToken.value += 1;
    },
    requestOpenCodePersona: () => {
      _openCodePersonaToken.value += 1;
    },
    requestOpenCodeContext: () => {
      _openCodeContextToken.value += 1;
    },
  };
}
