// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useTranscribedForwarding` — forwards voice-input transcripts to the
 * composer as `transcribed(text)` callbacks.
 *
 * Extracted from `VoiceInputBtn.vue` so the forwarding contract — which
 * is subtle and was the site of two production bugs — can be unit-tested
 * in isolation, without mounting the whole button component (mounting it
 * pulls in vue-i18n, the real mic-permission probe, and a shared
 * reactive surface whose cross-test interaction made `wrapper.emitted()`
 * accumulate counts across test fixtures — a brittle setup).
 *
 * Two source refs are forwarded to the SAME `transcribed` sink:
 *
 *   1. `transcript` — the END-OF-RECORDING full transcript. Emitted once
 *      per distinct non-empty value (the classic `text !== "" &&
 *      text !== prev` guard is correct here: the final transcript is set
 *      exactly once per session, so the `prev` guard only suppresses the
 *      idempotent re-assignment Vue might surface).
 *
 *   2. `partialTranscript` — the interim 3-second streaming chunks. Here
 *      the `text !== prev` guard MUST NOT be used:
 *
 *      Bug #1 (fixed) — Two consecutive interim chunks can legitimately
 *      recognise the SAME short phrase (user repeats "嗯嗯", "对对",
 *      "yes yes"). Gating on `text !== prev` silently dropped the second
 *      identical chunk. The fix: emit on EVERY non-empty write, then
 *      reset `partialTranscript` to "" immediately after emitting so the
 *      next write — even an identical one — is a fresh reactive
 *      transition Vue will notify on. The reset write itself does not
 *      re-emit because the `text !== ""` guard short-circuits it (no
 *      infinite loop).
 *
 * The producer-side companion fix for Bug #2 (a stale chunk arriving
 * after the user stopped recording) lives in `useVoiceInput.ts`
 * (a recording-generation guard); this consumer simply trusts that the
 * producer never writes a stale value into `partialTranscript`.
 */
import { watch, type Ref, type WatchStopHandle } from "vue";

export interface TranscribedForwardingSources {
  /** End-of-recording full transcript. */
  transcript: Ref<string>;
  /** Interim streaming chunk text (reset to "" by this composable). */
  partialTranscript: Ref<string>;
}

export interface TranscribedForwardingOptions {
  /** Called once per transcript / interim chunk that should be appended. */
  onTranscribed: (text: string) => void;
}

export interface TranscribedForwardingHandle {
  /** Stop both watchers (used by the host component on unmount). */
  stop: () => void;
}

/**
 * Wire the two transcript refs to the `onTranscribed` sink. Returns a
 * handle whose `stop()` tears down both watchers.
 */
export function useTranscribedForwarding(
  sources: TranscribedForwardingSources,
  opts: TranscribedForwardingOptions,
): TranscribedForwardingHandle {
  const { transcript, partialTranscript } = sources;
  const { onTranscribed } = opts;

  // Final end-of-recording transcript: emit once per distinct value.
  const stopTranscript: WatchStopHandle = watch(transcript, (text, prev) => {
    if (text !== "" && text !== prev) {
      onTranscribed(text);
    }
  });

  // Interim chunks: emit on every non-empty write (NO `!== prev` guard —
  // see Bug #1 in the file docstring), then reset to "" so identical
  // consecutive chunks each register as a fresh transition.
  const stopPartial: WatchStopHandle = watch(partialTranscript, (text) => {
    if (text !== "") {
      onTranscribed(text);
      partialTranscript.value = "";
    }
  });

  return {
    stop: () => {
      stopTranscript();
      stopPartial();
    },
  };
}
