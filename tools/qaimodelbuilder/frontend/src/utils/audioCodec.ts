// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Audio codec helpers — browser probe, PCM16 WAV encoding, AudioContext
 * constructor resolution.
 *
 * Behaviour source (V1 = validated):
 *   - `isChromeOrEdgeDesktop` → V1 `_isChromeOrEdgeDesktop`
 *     (`frontend/js/components/app-builder/inputs/AudioInput.js:32-43`)
 *   - `encodeWav` (+ inner `writeStr`) → V1 `encodeWav`
 *     (`AudioInput.js:48-82`) / `_encodeWav` (`useVoiceInput.js`)
 *   - `getAudioContextCtor` / `getOfflineAudioContextCtor` → V1's inline
 *     `window.AudioContext ?? window.webkitAudioContext` resolution.
 *
 * These helpers were previously duplicated verbatim in
 * `components/app-builder/inputs/AudioInput.vue` and
 * `composables/useVoiceInput.ts`. They are consolidated here as a focused,
 * side-effect-free pure module so both the App Builder audio dropzone and the
 * voice-input composable share one implementation (F5 dedup). The algorithms
 * are byte-for-byte identical to the previous copies — behaviour is unchanged.
 */

/**
 * `true` iff the current browser is desktop Chrome or Edge.
 *
 * Wrapped in `try/catch` (the more defensive of the two former copies) so a
 * hostile/absent `navigator` never throws — it just reports "not supported".
 */
export function isChromeOrEdgeDesktop(): boolean {
  try {
    if (typeof navigator === "undefined") return false;
    const ua = (navigator.userAgent || "").toLowerCase();
    if (/(android|iphone|ipad|ipod|mobile)/.test(ua)) return false;
    if (/opr\//.test(ua)) return false;
    return /chrome\//.test(ua) || /edg\//.test(ua);
  } catch {
    return false;
  }
}

/** PCM16 mono WAV encoder — same algorithm as V1 `encodeWav` / `_encodeWav`. */
export function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const numChannels = 1;
  const bytesPerSample = 2;
  const blockAlign = numChannels * bytesPerSample;
  const byteRate = sampleRate * blockAlign;
  const dataLen = samples.length * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataLen);
  const view = new DataView(buffer);

  function writeStr(off: number, s: string): void {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
  }

  writeStr(0, "RIFF");
  view.setUint32(4, 36 + dataLen, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true); // PCM chunk size
  view.setUint16(20, 1, true); // PCM format
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, byteRate, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, 16, true); // bits per sample
  writeStr(36, "data");
  view.setUint32(40, dataLen, true);

  let off = 44;
  for (let i = 0; i < samples.length; i++, off += 2) {
    let s = Math.max(-1, Math.min(1, samples[i] ?? 0));
    s = s < 0 ? s * 0x8000 : s * 0x7fff;
    view.setInt16(off, s, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

/** Resolve `AudioContext` (or the webkit-prefixed fallback). */
export function getAudioContextCtor(): typeof AudioContext | undefined {
  return (
    (window as unknown as { AudioContext?: typeof AudioContext })
      .AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext
  );
}

/** Resolve `OfflineAudioContext` (or the webkit-prefixed fallback). */
export function getOfflineAudioContextCtor():
  | typeof OfflineAudioContext
  | undefined {
  return (
    (window as unknown as { OfflineAudioContext?: typeof OfflineAudioContext })
      .OfflineAudioContext ??
    (
      window as unknown as {
        webkitOfflineAudioContext?: typeof OfflineAudioContext;
      }
    ).webkitOfflineAudioContext
  );
}
