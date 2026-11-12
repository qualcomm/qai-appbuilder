// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Whether the UI offers sampling-parameter controls (temperature / top_p /
 * max_tokens): the chat composer's Params pill + popover, and the per-model
 * "supported parameters" switches in Settings -> Cloud Models.
 *
 * Off in this build: nobody needs to tune them, and the backend resolves the
 * right values per model family (and drops params a model rejects). Only the
 * ENTRY POINTS are hidden - tab state (`modelParams`, default `useDefaults:
 * true` = send nothing) and already-saved `models[].params` stay intact, so
 * flipping this back to `true` restores the previous behaviour exactly.
 */
export const SHOW_SAMPLING_PARAMS = false;
