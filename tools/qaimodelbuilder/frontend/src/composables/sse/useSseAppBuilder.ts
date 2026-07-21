// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useSseAppBuilder — SSE composable for the app_builder context.
 *
 * Per api-contract section 3.3, app_builder streams emit:
 *
 *   event: state         data: { run_id, phase, status, ... }
 *   event: frame         data: { run_id, frame_type, ... }
 *   event: error         data: ApiErrorPayload
 *   event: done          data: { run_id, ... }
 *
 * `state` carries lifecycle transitions (queued -> running -> done),
 * while `frame` carries inner workflow data (model output, metrics,
 * intermediate artifacts). The composable exposes both as separate
 * reactive refs so SFCs can render them independently.
 */
import { ref, type Ref } from "vue";

import { useSse, type UseSseOptions, type UseSseReturn } from "./useSse";

export interface AppBuilderStateFrame {
  readonly run_id?: string;
  readonly phase?: string;
  readonly status?: string;
  readonly [key: string]: unknown;
}

export interface AppBuilderDataFrame {
  readonly run_id?: string;
  readonly frame_type?: string;
  readonly [key: string]: unknown;
}

export interface UseSseAppBuilderReturn extends UseSseReturn {
  /** Latest `event: state` payload (replaced each frame). */
  readonly state_frame: Ref<AppBuilderStateFrame | null>;
  /** Append-only list of `event: frame` payloads. */
  readonly frames: Ref<readonly AppBuilderDataFrame[]>;
  readonly resetFrames: () => void;
}

export function useSseAppBuilder(
  path: string | (() => string),
  opts: UseSseOptions = {},
): UseSseAppBuilderReturn {
  const state_frame: Ref<AppBuilderStateFrame | null> = ref(null);
  const frames: Ref<AppBuilderDataFrame[]> = ref([]);

  const base = useSse(
    path,
    () => ({
      onState: (data) => {
        state_frame.value = (data ?? {}) as AppBuilderStateFrame;
      },
      onFrame: (data) => {
        frames.value = [...frames.value, (data ?? {}) as AppBuilderDataFrame];
      },
    }),
    opts,
  );

  function resetFrames(): void {
    frames.value = [];
    state_frame.value = null;
  }

  return {
    state: base.state,
    lastError: base.lastError,
    open: base.open,
    close: base.close,
    state_frame,
    frames,
    resetFrames,
  };
}
