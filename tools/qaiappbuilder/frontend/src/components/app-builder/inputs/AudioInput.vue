<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * AudioInput — rich audio dropzone for the App Builder stage.
 *
 * V1 parity (ported from `frontend/js/components/app-builder/inputs/AudioInput.js`,
 * 707 lines) but rewritten in TS + Vue3 `<script setup>` with V2 architecture:
 *
 *   - Unified dropzone (drag / click / record / URL hint) when no audio is
 *     selected and not recording (V1 template L628-649).
 *   - Recording via `getUserMedia` → `ScriptProcessorNode` PCM capture →
 *     resample to 16 kHz mono → PCM16 WAV (V1 `startRecord` /
 *     `_onPcmCaptureStop` / `encodeWav`, L207-438), with a `MediaRecorder`
 *     fallback (V1 `_initMediaRecorderFallback` / `_onMediaRecorderStop`,
 *     L294-523) and a live waveform meter (V1 `_meterLoop`, L440-474).
 *   - Selected-audio card with `<audio controls>` + Clear (V1 L686-692).
 *   - Inline error band + "Uploading…" hint (V1 L680-697).
 *
 * V2 contract differences vs V1:
 *   - Upload endpoint is `/api/app-builder/upload/audio` (hyphenated) and the
 *     response is `{ artifact: { path, size_bytes, kind, checksum } }`; we
 *     emit `artifact.path` (a logical relative path string), NOT a data-URL.
 *   - `modelValue` is `string | null` (the audio path) to stay compatible
 *     with the overlay's `setInputKey('audio', $event)` wiring — we do NOT
 *     switch to V1's `{ audio: path }` object shape.
 *   - Since V2 has no static byte-serving route for freshly-uploaded inputs
 *     (run-scoped artifacts go through `store.artifactBlobUrl`, which is not
 *     applicable to a not-yet-run input), we keep the just-uploaded
 *     Blob locally and preview it via `URL.createObjectURL`. The emitted
 *     value is still the backend path string for the run to consume.
 */
import { ref, computed, onBeforeUnmount } from "vue";
import { useI18n } from "vue-i18n";

import { apiUpload, ApiError } from "@/api";
import {
  isChromeOrEdgeDesktop,
  encodeWav,
  getAudioContextCtor,
  getOfflineAudioContextCtor,
} from "@/utils/audioCodec";

// ─── i18n ──────────────────────────────────────────────────────────────────
const { t } = useI18n();

// ─── Types ───────────────────────────────────────────────────────────────────

interface AudioConstraints {
  sampleRate?: number;
  channels?: number;
  /** Max recording duration in seconds (default 120). */
  maxSec?: number;
  /** Allowed file extensions, e.g. ["wav", "mp3"]. */
  formats?: string[];
  /** Set false to disable the microphone entirely. */
  allowMic?: boolean;
}

interface Props {
  modelValue?: string | null;
  constraints?: AudioConstraints;
}

const props = withDefaults(defineProps<Props>(), {
  modelValue: null,
  constraints: () => ({}),
});

const emit = defineEmits<{
  "update:modelValue": [value: string | null];
}>();

/** Upload response shape (mirrors backend `UploadAudioResponse`). */
interface UploadAudioResponse {
  artifact: {
    path: string;
    size_bytes: number;
    kind: string;
    checksum?: string | null;
  };
}

// ─── Constants ───────────────────────────────────────────────────────────────

const TARGET_SAMPLE_RATE = 16000;
const WAVEFORM_BAR_COUNT = 40;
const DEFAULT_MAX_SEC = 120;

// Audio codec helpers (browser probe / WAV encoder / AudioContext ctor
// resolution) are shared with `useVoiceInput` via `@/utils/audioCodec`.

// ─── State ─────────────────────────────────────────────────────────────────────

const fileInputEl = ref<HTMLInputElement | null>(null);
const uploading = ref(false);
const errorText = ref("");
const dragOver = ref(false);

const recording = ref(false);
const recordSecs = ref(0);
const waveformBars = ref<number[]>([]);

/** Local preview URL for the just-selected / just-recorded audio (blob:). */
const previewUrl = ref<string | null>(null);

const currentPath = computed(() => props.modelValue ?? "");
const hasAudio = computed(() => currentPath.value !== "");

const maxSec = computed(() => {
  const m = props.constraints?.maxSec;
  return typeof m === "number" && m > 0 ? m : DEFAULT_MAX_SEC;
});

const canRecord = computed(() => {
  if (!isChromeOrEdgeDesktop()) return false;
  if (typeof navigator === "undefined") return false;
  if (
    navigator.mediaDevices === undefined ||
    typeof navigator.mediaDevices.getUserMedia !== "function"
  ) {
    return false;
  }
  if (typeof MediaRecorder === "undefined") return false;
  if (props.constraints?.allowMic === false) return false;
  return true;
});

// ─── Recording handles ──────────────────────────────────────────────────────────

let mediaStream: MediaStream | null = null;
let mediaRecorder: MediaRecorder | null = null;
let recordChunks: Blob[] = [];
let audioCtxLevel: AudioContext | null = null;
let analyser: AnalyserNode | null = null;
let meterRaf = 0;
let recordTimer: ReturnType<typeof setInterval> | null = null;
let waveformUpdateCounter = 0;

let audioCtxCapture: AudioContext | null = null;
let scriptProcessor: ScriptProcessorNode | null = null;
let pcmBuffers: Float32Array[] = [];
let pcmSampleRate = TARGET_SAMPLE_RATE;

// ─── Preview lifecycle ───────────────────────────────────────────────────────────

function setPreview(blob: Blob | null): void {
  if (previewUrl.value !== null) {
    try {
      URL.revokeObjectURL(previewUrl.value);
    } catch {
      // ignore
    }
    previewUrl.value = null;
  }
  if (blob !== null) {
    previewUrl.value = URL.createObjectURL(blob);
  }
}

// ─── File / drag upload (V1 L137-204) ────────────────────────────────────────────

function openFilePicker(): void {
  fileInputEl.value?.click();
}

async function onFileChange(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement;
  const f = input.files?.[0] ?? null;
  input.value = "";
  if (f === null) return;
  await uploadFile(f);
}

function onDragOver(): void {
  dragOver.value = true;
}

function onDragLeave(): void {
  dragOver.value = false;
}

async function onDrop(e: DragEvent): Promise<void> {
  dragOver.value = false;
  const f = e.dataTransfer?.files?.[0] ?? null;
  if (f === null) return;
  await uploadFile(f);
}

/** V1 `_validateFormat` (L165-172). */
function validateFormat(filename: string): string | null {
  const fmts = props.constraints?.formats;
  if (!Array.isArray(fmts) || fmts.length === 0) return null;
  const ext = (filename || "").split(".").pop()?.toLowerCase() ?? "";
  const ok = fmts.some(
    (fmt) => ext === String(fmt).toLowerCase().replace(/^\./, ""),
  );
  if (!ok) return t("appBuilder.audioInput.formatNotAllowed", { formats: fmts.join("/") });
  return null;
}

/** V1 `_uploadFile` (L174-204), adapted to V2 endpoint + response shape. */
async function uploadFile(file: File): Promise<void> {
  errorText.value = "";
  const ferr = validateFormat(file.name);
  if (ferr !== null) {
    errorText.value = ferr;
    return;
  }
  uploading.value = true;
  try {
    const fd = new FormData();
    fd.append("file", file, file.name || "audio.bin");
    const res = await apiUpload<UploadAudioResponse>(
      "/api/app-builder/upload/audio",
      fd,
    );
    const audioPath = res.artifact?.path ?? "";
    if (audioPath === "") {
      throw new Error("upload response missing artifact.path");
    }
    // Keep a local blob preview so the user can play back the just-uploaded
    // clip without a backend static route (V2 has none for inputs).
    setPreview(file);
    emit("update:modelValue", audioPath);
  } catch (err) {
    const msg =
      err instanceof ApiError ? err.message : ((err as Error).message ?? String(err));
    errorText.value = msg;
  } finally {
    uploading.value = false;
  }
}

// ─── Recording (V1 `startRecord`, L207-292) ──────────────────────────────────────

async function startRecord(): Promise<void> {
  if (recording.value) return;
  errorText.value = "";
  if (!canRecord.value) {
    errorText.value = t("appBuilder.audioInput.recordUnsupported");
    return;
  }

  // Clear any previously selected audio.
  if (hasAudio.value) {
    setPreview(null);
    emit("update:modelValue", null);
  }

  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        sampleRate: TARGET_SAMPLE_RATE,
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
    });
  } catch (err) {
    const e = err as { message?: string };
    errorText.value = `${t("appBuilder.audioInput.micDenied")}: ${e.message ?? String(err)}`;
    return;
  }

  // PCM capture path (V1 L240-279).
  try {
    const Ctor = getAudioContextCtor();
    if (Ctor === undefined) throw new Error("AudioContext unavailable");
    audioCtxCapture = new Ctor();
    pcmSampleRate = audioCtxCapture.sampleRate;

    if (audioCtxCapture.state === "suspended") {
      await audioCtxCapture.resume().catch(() => undefined);
    }

    const source = audioCtxCapture.createMediaStreamSource(mediaStream);

    analyser = audioCtxCapture.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    meterLoop();

    pcmBuffers = [];
    scriptProcessor = audioCtxCapture.createScriptProcessor(4096, 1, 1);
    scriptProcessor.onaudioprocess = (ev: AudioProcessingEvent): void => {
      const input = ev.inputBuffer.getChannelData(0);
      pcmBuffers.push(new Float32Array(input));
    };
    source.connect(scriptProcessor);
    scriptProcessor.connect(audioCtxCapture.destination);
  } catch {
    // Fall back to MediaRecorder (V1 L272-279).
    audioCtxCapture = null;
    scriptProcessor = null;
    pcmBuffers = [];
    initMediaRecorderFallback();
  }

  recordSecs.value = 0;
  recordTimer = setInterval(() => {
    recordSecs.value += 1;
    if (recordSecs.value >= maxSec.value) {
      void stopRecord();
    }
  }, 1000);

  recording.value = true;
}

/** V1 `_initMediaRecorderFallback` (L295-334). */
function initMediaRecorderFallback(): void {
  if (mediaStream === null) return;
  let mime = "audio/webm;codecs=opus";
  if (typeof MediaRecorder.isTypeSupported === "function") {
    if (!MediaRecorder.isTypeSupported(mime)) {
      mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
    }
  }
  try {
    mediaRecorder =
      mime !== ""
        ? new MediaRecorder(mediaStream, { mimeType: mime })
        : new MediaRecorder(mediaStream);
  } catch (err) {
    const e = err as { message?: string };
    errorText.value = `${t("appBuilder.audioInput.recorderInitFailed")}: ${e.message ?? String(err)}`;
    stopMediaStream();
    return;
  }

  recordChunks = [];
  mediaRecorder.ondataavailable = (ev: BlobEvent): void => {
    if (ev.data && ev.data.size > 0) recordChunks.push(ev.data);
  };
  mediaRecorder.onstop = () => {
    void onMediaRecorderStop();
  };

  // Level meter (only if not already created by the PCM path).
  if (analyser === null) {
    try {
      const Ctor = getAudioContextCtor();
      if (Ctor !== undefined) {
        audioCtxLevel = new Ctor();
        const src = audioCtxLevel.createMediaStreamSource(mediaStream);
        analyser = audioCtxLevel.createAnalyser();
        analyser.fftSize = 256;
        src.connect(analyser);
        meterLoop();
      }
    } catch {
      // level meter is non-critical
    }
  }

  mediaRecorder.start();
}

/** V1 `stopRecord` (L336-374). */
async function stopRecord(): Promise<void> {
  if (!recording.value) return;
  recording.value = false;

  if (recordTimer !== null) {
    clearInterval(recordTimer);
    recordTimer = null;
  }
  if (meterRaf !== 0) {
    cancelAnimationFrame(meterRaf);
    meterRaf = 0;
  }

  if (scriptProcessor !== null) {
    await onPcmCaptureStop();
    return;
  }

  if (mediaRecorder !== null) {
    try {
      mediaRecorder.stop();
    } catch {
      // onstop callback handles the rest
    }
  } else {
    errorText.value =
      t("appBuilder.audioInput.recordNotInitialized");
    cleanupRecord();
  }
}

/** V1 `_onPcmCaptureStop` (L377-438). */
async function onPcmCaptureStop(): Promise<void> {
  try {
    if (scriptProcessor !== null) {
      scriptProcessor.onaudioprocess = null;
      try {
        scriptProcessor.disconnect();
      } catch {
        // ignore
      }
    }

    let totalLen = 0;
    for (const buf of pcmBuffers) totalLen += buf.length;

    if (totalLen <= 0) {
      errorText.value =
        t("appBuilder.audioInput.emptyRecording");
      cleanupRecord();
      return;
    }

    let samples: Float32Array;
    if (pcmSampleRate === TARGET_SAMPLE_RATE) {
      samples = new Float32Array(totalLen);
      let offset = 0;
      for (const buf of pcmBuffers) {
        samples.set(buf, offset);
        offset += buf.length;
      }
    } else {
      const rawSamples = new Float32Array(totalLen);
      let offset = 0;
      for (const buf of pcmBuffers) {
        rawSamples.set(buf, offset);
        offset += buf.length;
      }
      const duration = totalLen / pcmSampleRate;
      const targetLen = Math.ceil(duration * TARGET_SAMPLE_RATE);
      const Ctor = getOfflineAudioContextCtor();
      if (Ctor === undefined) {
        // Degraded fallback: encode at source rate.
        samples = rawSamples;
        const wav = encodeWav(samples, pcmSampleRate);
        await finishRecordingUpload(wav);
        return;
      }
      const off = new Ctor(1, targetLen, TARGET_SAMPLE_RATE);
      const audioBuffer = off.createBuffer(1, totalLen, pcmSampleRate);
      audioBuffer.getChannelData(0).set(rawSamples);
      const src = off.createBufferSource();
      src.buffer = audioBuffer;
      src.connect(off.destination);
      src.start(0);
      const rendered = await off.startRendering();
      samples = rendered.getChannelData(0);
    }

    pcmBuffers = [];

    const wavBlob = encodeWav(samples, TARGET_SAMPLE_RATE);
    await finishRecordingUpload(wavBlob);
  } catch (err) {
    const e = err as { message?: string };
    errorText.value = `${t("appBuilder.audioInput.pipelineFailed")}: ${e.message ?? String(err)}`;
  } finally {
    cleanupRecord();
  }
}

/** V1 `_meterLoop` (L440-474). */
function meterLoop(): void {
  if (analyser === null) return;
  const buf = new Uint8Array(analyser.fftSize);
  waveformUpdateCounter = 0;
  waveformBars.value = [];
  const tick = (): void => {
    if (analyser === null) return;
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const v = ((buf[i] ?? 128) - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / buf.length);

    waveformUpdateCounter++;
    if (waveformUpdateCounter % 3 === 0) {
      const bars = waveformBars.value;
      const barHeight = Math.max(0.05, Math.min(1, rms * 3));
      if (bars.length >= WAVEFORM_BAR_COUNT) {
        waveformBars.value = [...bars.slice(1), barHeight];
      } else {
        waveformBars.value = [...bars, barHeight];
      }
    }

    meterRaf = requestAnimationFrame(tick);
  };
  tick();
}

/** V1 `_onMediaRecorderStop` (L476-523). */
async function onMediaRecorderStop(): Promise<void> {
  try {
    const blob = new Blob(recordChunks, {
      type: mediaRecorder?.mimeType ?? "audio/webm",
    });
    recordChunks = [];
    const arrayBuf = await blob.arrayBuffer();
    const Ctor = getAudioContextCtor();
    if (Ctor === undefined) {
      errorText.value = t("appBuilder.audioInput.audioContextUnavailable");
      cleanupRecord();
      return;
    }
    const decodeCtx = new Ctor();
    let decoded: AudioBuffer;
    try {
      decoded = await decodeCtx.decodeAudioData(arrayBuf.slice(0));
    } catch (err) {
      const e = err as { message?: string };
      errorText.value = `${t("appBuilder.audioInput.decodeFailed")}: ${e.message ?? String(err)}`;
      void decodeCtx.close().catch(() => undefined);
      cleanupRecord();
      return;
    }
    void decodeCtx.close().catch(() => undefined);

    const targetLen = Math.ceil(decoded.duration * TARGET_SAMPLE_RATE);
    if (targetLen <= 0) {
      errorText.value = t("appBuilder.audioInput.emptyRecording");
      cleanupRecord();
      return;
    }
    const OffCtor = getOfflineAudioContextCtor();
    if (OffCtor === undefined) {
      errorText.value = t("appBuilder.audioInput.offlineAudioUnavailable");
      cleanupRecord();
      return;
    }
    const off = new OffCtor(1, targetLen, TARGET_SAMPLE_RATE);
    const src = off.createBufferSource();
    src.buffer = decoded;
    src.connect(off.destination);
    src.start(0);
    const rendered = await off.startRendering();
    const wavBlob = encodeWav(
      rendered.getChannelData(0),
      TARGET_SAMPLE_RATE,
    );
    await finishRecordingUpload(wavBlob);
  } catch (err) {
    const e = err as { message?: string };
    errorText.value = `${t("appBuilder.audioInput.pipelineFailed")}: ${e.message ?? String(err)}`;
  } finally {
    cleanupRecord();
  }
}

/** Shared tail: wrap a recorded WAV blob in a File and upload it. */
async function finishRecordingUpload(wavBlob: Blob): Promise<void> {
  const fname = `record-${Date.now()}.wav`;
  const file = new File([wavBlob], fname, { type: "audio/wav" });
  await uploadFile(file);
}

/** V1 `_cleanupRecord` (L525-553). */
function cleanupRecord(): void {
  stopMediaStream();
  if (audioCtxLevel !== null) {
    void audioCtxLevel.close().catch(() => undefined);
    audioCtxLevel = null;
  }
  if (audioCtxCapture !== null) {
    void audioCtxCapture.close().catch(() => undefined);
    audioCtxCapture = null;
  }
  if (scriptProcessor !== null) {
    try {
      scriptProcessor.disconnect();
    } catch {
      // ignore
    }
    scriptProcessor = null;
  }
  pcmBuffers = [];
  analyser = null;
  if (meterRaf !== 0) {
    cancelAnimationFrame(meterRaf);
    meterRaf = 0;
  }
  if (recordTimer !== null) {
    clearInterval(recordTimer);
    recordTimer = null;
  }
  mediaRecorder = null;
  waveformBars.value = [];
  recording.value = false;
}

/** V1 `_stopMediaStream` (L555-562). */
function stopMediaStream(): void {
  if (mediaStream !== null) {
    try {
      for (const t of mediaStream.getTracks()) t.stop();
    } catch {
      // ignore
    }
    mediaStream = null;
  }
}

// ─── Clear (V1 `onClear`, L571-575) ────────────────────────────────────────────

function onClear(): void {
  errorText.value = "";
  setPreview(null);
  emit("update:modelValue", null);
}

onBeforeUnmount(() => {
  try {
    if (recording.value) void stopRecord();
  } catch {
    // ignore
  }
  cleanupRecord();
  setPreview(null);
});
</script>

<template>
  <div class="ab-audio-input">
    <!-- Unified dropzone (no audio selected, not recording) -->
    <div
      v-if="!hasAudio && !recording"
      class="ab-audio-dropzone"
      :class="{ 'is-dragover': dragOver }"
      role="button"
      tabindex="0"
      @click="openFilePicker"
      @keydown.enter.prevent="openFilePicker"
      @keydown.space.prevent="openFilePicker"
      @dragover.prevent="onDragOver"
      @dragleave.prevent="onDragLeave"
      @drop.prevent="onDrop"
    >
      <div
        class="ab-audio-dropzone__glyph"
        aria-hidden="true"
      >
        🎙️
      </div>
      <p class="ab-audio-dropzone__main">
        {{ t("appBuilder.audioDropzone.main") }}
      </p>
      <p class="ab-audio-dropzone__hint">
        {{ t("appBuilder.audioDropzone.hint") }}
      </p>
      <div
        class="ab-audio-dropzone__actions"
        @click.stop
      >
        <button
          type="button"
          class="ab-audio-btn"
          @click="openFilePicker"
        >
          {{ t("appBuilder.audioUpload") }}
        </button>
        <button
          v-if="canRecord"
          type="button"
          class="ab-audio-btn"
          @click="startRecord"
        >
          {{ t("appBuilder.audioRecord") }}
        </button>
      </div>
    </div>

    <!-- Recording: stop button -->
    <div
      v-if="recording"
      class="ab-audio-stop-row"
    >
      <button
        type="button"
        class="ab-audio-btn ab-audio-btn--danger"
        :disabled="uploading"
        @click="stopRecord"
      >
        <span aria-hidden="true">■</span>
        {{ t("appBuilder.audioStop") }} · {{ recordSecs }}s
      </button>
    </div>

    <!-- Browser-not-supported hint -->
    <div
      v-if="!canRecord && !hasAudio && !recording"
      class="ab-audio-browser-hint"
    >
      {{ t("appBuilder.audioOnlyChromeEdge") }}
    </div>

    <!-- Recording: waveform visualization -->
    <div
      v-if="recording"
      class="ab-audio-waveform"
    >
      <div class="ab-audio-waveform__bars">
        <div
          v-for="(bar, idx) in waveformBars"
          :key="idx"
          class="ab-audio-waveform__bar"
          :style="{ height: (bar * 100).toFixed(0) + '%' }"
        />
      </div>
      <div class="ab-audio-waveform__label">
        <span class="ab-audio-rec-dot" />
        {{ t("appBuilder.audioRecording") }} ({{ recordSecs }}s)
      </div>
    </div>

    <!-- Uploading -->
    <div
      v-if="uploading"
      class="ab-audio-uploading"
    >
      {{ t("appBuilder.uploading") }}
    </div>

    <!-- Selected audio: path + player + clear -->
    <div
      v-if="hasAudio && !recording"
      class="ab-audio-current"
    >
      <div
        class="ab-audio-path"
        :title="currentPath"
      >
        {{ currentPath }}
      </div>
      <audio
        v-if="previewUrl"
        :src="previewUrl"
        controls
        preload="metadata"
        class="ab-audio-player"
      />
      <button
        type="button"
        class="ab-audio-btn ab-audio-btn--ghost"
        :disabled="uploading || recording"
        @click="onClear"
      >
        {{ t("appBuilder.clear") }}
      </button>
    </div>

    <!-- Error band -->
    <div
      v-if="errorText"
      class="ab-audio-error"
      role="alert"
    >
      <span aria-hidden="true">⚠</span> {{ errorText }}
    </div>

    <!-- Hidden file input -->
    <input
      ref="fileInputEl"
      type="file"
      accept="audio/*"
      class="ab-audio-file"
      @change="onFileChange"
    />
  </div>
</template>

<style scoped>
.ab-audio-input {
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-family: var(--ab-font, inherit);
  color: var(--ab-text, var(--text-primary));
}

/* ── Dropzone ─────────────────────────────────────────────── */
.ab-audio-dropzone {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 24px 16px;
  border: 1.5px dashed var(--ab-border, var(--border));
  border-radius: 12px;
  background: var(--ab-panel-2, var(--bg-secondary));
  cursor: pointer;
  text-align: center;
  transition:
    border-color 0.15s ease,
    background 0.15s ease;
}

.ab-audio-dropzone:hover,
.ab-audio-dropzone:focus-visible {
  border-color: var(--ab-accent, var(--accent));
  outline: none;
}

.ab-audio-dropzone.is-dragover {
  border-color: var(--ab-accent, var(--accent));
  background: var(--ab-panel-3, var(--bg-tertiary));
}

.ab-audio-dropzone__glyph {
  font-size: 28px;
  line-height: 1;
}

.ab-audio-dropzone__main {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--ab-text, var(--text-primary));
}

.ab-audio-dropzone__hint {
  margin: 0;
  font-size: 12px;
  color: var(--ab-text-muted, var(--text-muted));
}

.ab-audio-dropzone__actions {
  display: flex;
  gap: 8px;
  margin-top: 6px;
}

/* ── Buttons ──────────────────────────────────────────────── */
.ab-audio-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border: 1px solid var(--ab-border, var(--border));
  border-radius: 8px;
  background: var(--ab-surface, var(--bg-tertiary));
  color: var(--ab-text, var(--text-primary));
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition:
    background 0.15s ease,
    border-color 0.15s ease;
}

.ab-audio-btn:hover:not(:disabled) {
  border-color: var(--ab-accent, var(--accent));
  background: var(--ab-surface-alt, var(--bg-hover));
}

.ab-audio-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.ab-audio-btn--danger {
  border-color: var(--ab-error, var(--danger));
  color: var(--ab-error, var(--danger));
}

.ab-audio-btn--danger:hover:not(:disabled) {
  border-color: var(--ab-error, var(--danger));
  background: color-mix(
    in srgb,
    var(--ab-error, var(--danger)) 12%,
    transparent
  );
}

.ab-audio-btn--ghost {
  background: transparent;
}

/* ── Recording controls ───────────────────────────────────── */
.ab-audio-stop-row {
  display: flex;
}

.ab-audio-waveform {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px;
  border: 1px solid var(--ab-border, var(--border));
  border-radius: 10px;
  background: var(--ab-panel-2, var(--bg-secondary));
}

.ab-audio-waveform__bars {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 48px;
}

.ab-audio-waveform__bar {
  flex: 1 1 auto;
  min-width: 2px;
  min-height: 2px;
  border-radius: 2px;
  background: var(--ab-accent, var(--accent));
  transition: height 0.05s linear;
}

.ab-audio-waveform__label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--ab-text-muted, var(--text-muted));
}

.ab-audio-rec-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--ab-error, var(--danger));
  animation: ab-audio-blink 1s ease-in-out infinite;
}

@keyframes ab-audio-blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.3;
  }
}

/* ── Browser hint / uploading ─────────────────────────────── */
.ab-audio-browser-hint,
.ab-audio-uploading {
  font-size: 12px;
  color: var(--ab-text-muted, var(--text-muted));
}

/* ── Selected audio ───────────────────────────────────────────
 * Intentionally NO scoped rules here: the selected-audio card reuses the
 * GLOBAL `.ab-audio-current` / `.ab-audio-path` / `.ab-audio-player` classes
 * from styles/app-builder/app-builder.css (V1 parity — centered, transparent,
 * native-width player). A previous BEM `.ab-audio-current__*` override (card +
 * border + `width:100%`) drifted from the global `--ab-*` token system and was
 * removed per "reuse > recreate". */

/* ── Error band ───────────────────────────────────────────── */
.ab-audio-error {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  border: 1px solid var(--ab-error, var(--danger));
  border-radius: 8px;
  font-size: 12px;
  color: var(--ab-error, var(--danger));
  background: color-mix(
    in srgb,
    var(--ab-error, var(--danger)) 8%,
    transparent
  );
}

.ab-audio-file {
  display: none;
}
</style>
