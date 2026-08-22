// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useVoiceInput` — voice-to-text composable.
 *
 * T2.6-A: ported from V1 `frontend/js/composables/useVoiceInput.js`
 * (928 lines) and adapted to the V2 architecture:
 *
 *   1. PCM capture via `getUserMedia` + `AudioContext` + `ScriptProcessorNode`
 *   2. Resample to 16 kHz mono and encode to PCM16 WAV
 *   3. POST multipart `/api/app-builder/upload/audio` → `{artifact:{path,...}}`
 *   4. POST `/api/app-builder/runs` with `{model_id, inputs:{audio, variant_id}}`
 *   5. GET SSE `/api/app-builder/runs/{run_id}/stream`, parse the
 *      terminal `frame` event whose payload contains the transcript
 *      `output.fullText`
 *
 * The composable reads / writes the user's preferred ASR engine
 * (whisper-base / zipformer-zh / …) via:
 *   GET / PUT `/api/app-builder/voice-preference`
 *
 * `available` is driven by `voice-preference.enabled` + browser MediaRecorder
 * support. `start()` / `stop()` / `cancel()` are the public actions.
 */
import { ref, computed, onBeforeUnmount } from "vue";
import { useI18n } from "vue-i18n";

import {
  apiJson,
  apiWsStream,
  apiUpload,
  ApiError,
  apiBaseUrl,
} from "@/api";
import { readCsrfCookie, QAI_CSRF_HEADER } from "@/api/csrf";
import { useToast } from "@/composables/useToast";
import {
  isChromeOrEdgeDesktop,
  encodeWav,
  getAudioContextCtor,
  getOfflineAudioContextCtor,
} from "@/utils/audioCodec";

// ─── Types ───────────────────────────────────────────────────────────────────

export interface VoiceEngine {
  /** Stable id used as the persistence key. */
  id: string;
  /** App-builder model id. */
  modelId: string;
  /** Variant id (passed into `inputs.variant_id`). */
  variantId: string;
  /** Display label shown on the toolbar pill. */
  label: string;
  /** i18n key that overrides label when present. */
  labelKey: string;
  /** ISO-639-1 language hint (used for params on the run). */
  lang: string;
  /** Optional run params (passed through to RunCreateRequest.inputs.params). */
  params: Record<string, unknown>;
}

interface VoicePreferenceResponse {
  enabled: boolean;
  preferred_model_id: string | null;
  preferred_variant_id?: string | null;
}

/**
 * One entry of `GET /api/app-builder/worker/status` `loaded_models` array.
 *
 * Mirrors the backend DTO (`LoadedModelDTO` in
 * `interfaces/http/routes/app_builder.py`) — snake_case is the wire shape;
 * we keep it as-is here and adapt at the boundary in `refreshWorkerStatus`.
 */
interface LoadedModelDTOWire {
  model_id: string;
  variant_id: string | null;
  last_used_at?: string | null;
  age_seconds?: number | null;
  state?: string | null;
}

/** Adapted shape exposed to consumers (V1 parity — camelCase + minimal). */
export interface LoadedModelInfo {
  modelId: string;
  variantId: string | null;
  lastUsedAt: string | null;
  ageSeconds: number | null;
  state: string | null;
}

interface WorkerStatusResponse {
  total_workers?: number;
  busy_workers?: number;
  queued_runs?: number;
  alive?: boolean;
  state?: string;
  active_model_id?: string | null;
  multimodel?: boolean;
  loaded_models?: readonly LoadedModelDTOWire[];
}

/**
 * Cold/Loading/Ready state of the *current* preload attempt for the active
 * engine. `'idle'` means no preload has been kicked off; `'loading'` is
 * after `triggerPreload` started but before `loadedModels` reflects it;
 * `'warm'` after the worker confirms the model is loaded; `'failed'` if
 * the preload returned `failed` or threw.
 */
export type VoicePreloadState = "idle" | "loading" | "warm" | "failed";

interface UploadAudioResponse {
  artifact: {
    path: string;
    size_bytes: number;
    kind: string;
    checksum?: string | null;
  };
}

/** ``POST /api/app-builder/voice-input/preload`` response body. */
interface PreloadResultResponse {
  status: string;
  model_id?: string | null;
  variant_id?: string | null;
  detail?: string | null;
}

interface RunResponse {
  id: string;
  model_id: string;
  status: string;
  artifacts: Array<{
    path: string;
    size_bytes?: number;
    kind?: string;
  }>;
  error_message?: string | null;
  // PR-F1 (F-15) — append-only structured failure code mirrored from
  // ``Run.error_code`` / runner NDJSON ``error.code``. ``"WEIGHTS_NOT_INSTALLED"``
  // → toast `voiceInput.weightsMissing`; ``"AUDIO_DECODE_ERROR"`` /
  // ``"INVALID_INPUT"`` → `voiceInput.encodeFailed`; default →
  // `voiceInput.inferenceFailed`. Field is absent on pre-PR-F1 backends
  // (read with `??`-guard).
  error_code?: string | null;
}

/** SSE `frame` event payload shape — sequence + payload. */
interface RunFrameEnvelope {
  sequence?: number;
  payload?: unknown;
}

interface RunStateEnvelope {
  status?: string;
  run_id?: string;
  ts?: string;
}

export type VoiceState = "idle" | "recording" | "processing" | "error";

/**
 * How many seconds of audio to accumulate before firing an interim
 * transcription during a live recording session.
 */
export const INTERIM_CHUNK_SECS = 3;

export interface UseVoiceInputOptions {
  /** Max recording duration in seconds. Defaults to 120. */
  maxDurationSecs?: number;
}

// ─── Constants ───────────────────────────────────────────────────────────────

const TARGET_SAMPLE_RATE = 16000;
// 2026-06-21: removed STORAGE_KEY_ENGINE — engineId is no longer persisted to
// localStorage. The DB (``app_builder_voice_pref``) is now the single source of
// truth for ``enabled / preferred_model_id / preferred_variant_id``; the UI
// derives ``engineId`` from ``refreshPreference()`` on first mount and writes
// every ``setEngine`` change straight back through ``persistPreference()``.
// Eliminates the localStorage ↔ DB drift that was leaving ``enabled=False`` in
// the DB while the toolbar showed a selected engine.

/**
 * How long `triggerPreload` polls `worker/status` for the selected engine to
 * become resident before giving up. Aligned with the backend warm-load budget
 * (`StickyWorkerHost.LOAD_MODEL_TIMEOUT_S` / `warm_load_model_into_host`'s
 * `wait_for(..., 120.0)`) plus a small grace, so a slow cold Whisper load that
 * the backend is still completing reliably flips the dot to "warm" instead of
 * spinning forever (the old 30s window was shorter than a cold load).
 */
const PRELOAD_POLL_BUDGET_MS = 125_000;

// Engine catalog. Mirrors V1's two-engine setup; `whisper-base` is the
// English-only variant from the QAI app-builder pack manifests, and
// `zipformer-zh` is the Chinese engine. The display labels are intentionally
// honest (English vs Chinese) so users don't pick the wrong one and get
// empty transcripts.
export const VOICE_ENGINES: readonly VoiceEngine[] = [
  {
    id: "whisper-base",
    modelId: "whisper-base",
    variantId: "fp16",
    label: "Whisper (English)",
    labelKey: "voiceInput.engine.whisper",
    lang: "en",
    params: { vad: true },
  },
  {
    id: "zipformer-zh",
    modelId: "zipformer-zh",
    variantId: "int8",
    label: "Zipformer (Chinese)",
    labelKey: "voiceInput.engine.zipformer",
    lang: "zh",
    params: { vad: true, language: "zh" },
  },
];

// ─── Helpers ─────────────────────────────────────────────────────────────────

// Audio codec helpers (browser probe / WAV encoder / AudioContext ctor
// resolution) are shared with `AudioInput.vue` via `@/utils/audioCodec`.

/** Resample to 16 kHz mono using OfflineAudioContext. */
async function pcmToTargetWav(
  pcmBuffers: Float32Array[],
  srcSampleRate: number,
): Promise<Blob | null> {
  let totalLen = 0;
  for (const b of pcmBuffers) totalLen += b.length;
  if (totalLen <= 0) return null;

  if (srcSampleRate === TARGET_SAMPLE_RATE) {
    const samples = new Float32Array(totalLen);
    let offset = 0;
    for (const b of pcmBuffers) {
      samples.set(b, offset);
      offset += b.length;
    }
    return encodeWav(samples, TARGET_SAMPLE_RATE);
  }

  const raw = new Float32Array(totalLen);
  let offset = 0;
  for (const b of pcmBuffers) {
    raw.set(b, offset);
    offset += b.length;
  }
  const duration = totalLen / srcSampleRate;
  const targetLen = Math.ceil(duration * TARGET_SAMPLE_RATE);
  const Ctor = getOfflineAudioContextCtor();
  if (Ctor === undefined) {
    // Fallback: encode at source rate; backend will still accept arbitrary
    // sample rate WAVs — this is a degraded but functional path.
    return encodeWav(raw, srcSampleRate);
  }
  const off = new Ctor(1, targetLen, TARGET_SAMPLE_RATE);
  const audioBuffer = off.createBuffer(1, totalLen, srcSampleRate);
  audioBuffer.getChannelData(0).set(raw);
  const src = off.createBufferSource();
  src.buffer = audioBuffer;
  src.connect(off.destination);
  src.start(0);
  const rendered = await off.startRendering();
  return encodeWav(rendered.getChannelData(0), TARGET_SAMPLE_RATE);
}

// ─── Module-level singletons (shared across all useVoiceInput callers) ─────
// Each composable instance produces its own recording state machine, but the
// engine selection + backend-enabled probe are global (the user has one
// preferred ASR engine app-wide). Without this, the toolbar pill, the
// engine popover, and the mic button each held their own copy of `engineId`
// and never observed changes made by the others.
//
// 2026-06-21: ``_engineIdSingleton`` no longer reads from localStorage. The
// DB is the single source of truth (``GET /api/app-builder/voice-preference``
// → ``preferred_model_id`` + ``preferred_variant_id`` → reverse-looked-up to
// a ``VOICE_ENGINES[i].id`` in ``refreshPreference``). The initial value here
// is the catalog default (zipformer-zh, the Chinese engine, matching the
// backend ``VoiceInputPreference.default()``); it gets overwritten by the
// first ``refreshPreference`` round-trip on mount (typically <50 ms).

const _DEFAULT_ENGINE_ID =
  VOICE_ENGINES.find((e) => e.id === "zipformer-zh")?.id ??
  VOICE_ENGINES[0]!.id;

const _engineIdSingleton = ref<string>(_DEFAULT_ENGINE_ID);
const _backendEnabledSingleton = ref<boolean>(true);
let _initialPrefRefreshed = false;

// Worker-status singletons. These mirror what the popover and the toolbar
// pill both render (Ready / Loading / Cold dots), so they MUST be shared
// across composable instances — otherwise opening the popover and clicking
// the mic button would each see their own snapshot.
const _loadedModelsSingleton = ref<readonly LoadedModelInfo[]>([]);
const _preloadStateSingleton = ref<VoicePreloadState>("idle");
/** In-flight guard — prevents concurrent worker/status GETs from stacking. */
let _workerStatusInflight: Promise<void> | null = null;
/** Active long-poll cancel token. New polls invalidate older ones. */
let _workerPollToken = 0;

// ─── Composable ──────────────────────────────────────────────────────────────

export function useVoiceInput(opts: UseVoiceInputOptions = {}) {
  const toast = useToast();
  // PR-F1 (F-15) — vue-i18n hook for structured error → friendly toast
  // dispatch. Mirrors V1 ``useVoiceInput.js`` ``_t(...)`` calls (L744-775)
  // but uses the project's vue-i18n setup (composables/useChatComposer
  // pattern). MUST be called inside ``setup`` (the consumer is
  // ``ChatComposer.vue``, which already has an i18n root injected).
  const { t } = useI18n();
  const state = ref<VoiceState>("idle");
  const recordSecs = ref(0);
  const meterLevel = ref(0); // 0..1 RMS
  const transcript = ref("");
  /**
   * Fires once per 10-second interim chunk while recording is active.
   * Each emission is a single recognised segment; consumers (VoiceInputBtn)
   * should append it to the composer textarea immediately rather than
   * waiting for the full-recording `transcript`.
   */
  const partialTranscript = ref("");
  const errorText = ref("");

  const isListening = computed(() => state.value === "recording");
  const isProcessing = computed(() => state.value === "processing");
  const isBusy = computed(
    () => state.value === "recording" || state.value === "processing",
  );

  const browserSupported = ref(
    typeof navigator !== "undefined" &&
      navigator.mediaDevices !== undefined &&
      typeof navigator.mediaDevices.getUserMedia === "function" &&
      isChromeOrEdgeDesktop(),
  );

  /** Backend-side enabled flag; refreshed once per app session. */
  const backendEnabled = _backendEnabledSingleton;
  /** True iff browser supports recording AND backend voice-input is enabled. */
  const available = computed(
    () => browserSupported.value && backendEnabled.value,
  );

  // ─── Engine selection (DB-backed; refreshPreference reverse-looks-up) ─────

  const engineId = _engineIdSingleton;
  const engine = computed<VoiceEngine>(
    () =>
      VOICE_ENGINES.find((e) => e.id === engineId.value) ?? VOICE_ENGINES[0]!,
  );

  async function persistPreference(eng: VoiceEngine): Promise<void> {
    try {
      await apiJson<VoicePreferenceResponse>(
        "PUT",
        "/api/app-builder/voice-preference",
        {
          enabled: true,
          preferred_model_id: eng.modelId,
          // V1 parity: pin the exact variant so the next-boot warm-up loads
          // the variant the user picked (whisper→fp16 / zipformer→int8), not
          // just the model.
          preferred_variant_id: eng.variantId,
        },
      );
    } catch {
      // non-critical: next-time warm-up just won't happen
    }
  }

  async function setEngine(id: string): Promise<void> {
    if (!VOICE_ENGINES.some((e) => e.id === id)) return;
    // 2026-06-21: do NOT early-return when ``id === engineId.value``. The
    // earlier "skip-if-same" optimisation defeated the *only* code path that
    // writes ``enabled=true`` back to the DB — clicking the already-selected
    // engine should still synchronise DB state in case it drifted (e.g. fresh
    // install where the singleton's in-memory default was never confirmed
    // against persistent storage). DB is the single source of truth.
    engineId.value = id;
    const eng = VOICE_ENGINES.find((e) => e.id === id);
    if (eng !== undefined) {
      // Race fix: await the preference PUT before kicking off the preload.
      // Although `triggerPreload` is now parameter-driven (it POSTs the
      // selected engine directly, so it no longer depends on the DB
      // `preferred_model_id`), we still persist-then-preload so the DB
      // preference is correct for the *startup* warm-up path, and so a
      // preload that races the PUT can never read a stale preference.
      await persistPreference(eng);
      await triggerPreload();
    }
  }

  // ─── Initial backend probe ───────────────────────────────────────────────

  async function refreshPreference(): Promise<void> {
    try {
      const pref = await apiJson<VoicePreferenceResponse>(
        "GET",
        "/api/app-builder/voice-preference",
      );
      _backendEnabledSingleton.value = Boolean(pref.enabled);
      // 2026-06-21: DB is the single source of truth for engine selection.
      // Reverse-look-up the engine from ``preferred_model_id`` (and tie-break
      // on ``preferred_variant_id`` when the same model has multiple variants
      // — e.g. whisper-base's fp16 vs int8) and assign it back to the
      // singleton so the toolbar pill, the popover and the mic button all
      // agree from first paint. Falls through silently when the DB value
      // doesn't map to a known engine (e.g. a stale row from an older catalog
      // version) — the singleton then keeps its catalog default.
      const modelId = pref.preferred_model_id;
      const variantId = pref.preferred_variant_id ?? null;
      if (modelId !== null && modelId !== undefined) {
        const matched =
          VOICE_ENGINES.find(
            (e) =>
              e.modelId === modelId &&
              (variantId === null || e.variantId === variantId),
          ) ?? VOICE_ENGINES.find((e) => e.modelId === modelId);
        if (matched !== undefined) {
          _engineIdSingleton.value = matched.id;
        }
      }
    } catch {
      // Treat any failure as "enabled" so the UI surfaces the real button;
      // the actual record / transcribe flow will surface its own error if
      // the backend disagrees. The engine singleton keeps its catalog
      // default (zipformer-zh) — matching the backend's ``default()``.
      _backendEnabledSingleton.value = true;
    }
  }

  // First mount in the page does the network round-trip; later mounts re-use
  // the singleton state.
  if (!_initialPrefRefreshed) {
    _initialPrefRefreshed = true;
    void refreshPreference();
  }

  // ─── Preload (warm sticky-worker) ────────────────────────────────────────

  /**
   * GET `/api/app-builder/worker/status` and update the shared
   * `loadedModels` snapshot. The worker/status DTO is locked under §3.1 —
   * we don't write to it, only adapt snake_case → camelCase for callers.
   */
  async function refreshWorkerStatus(): Promise<void> {
    if (_workerStatusInflight !== null) {
      await _workerStatusInflight;
      return;
    }
    const p = (async (): Promise<void> => {
      try {
        const data = await apiJson<WorkerStatusResponse>(
          "GET",
          "/api/app-builder/worker/status",
        );
        const wire = Array.isArray(data?.loaded_models)
          ? data.loaded_models
          : [];
        _loadedModelsSingleton.value = wire.map((w) => ({
          modelId: w.model_id,
          variantId: w.variant_id ?? null,
          lastUsedAt: w.last_used_at ?? null,
          ageSeconds:
            typeof w.age_seconds === "number" ? w.age_seconds : null,
          state: w.state ?? null,
        }));
        // If the currently selected engine is now in loaded_models, flip
        // preloadState to 'warm' so the dots reflect reality.
        const cur = _engineIdSingleton.value;
        const eng = VOICE_ENGINES.find((e) => e.id === cur);
        if (
          eng !== undefined &&
          _loadedModelsSingleton.value.some((m) => m.modelId === eng.modelId)
        ) {
          _preloadStateSingleton.value = "warm";
        }
      } catch {
        // soft-fail — leave previous snapshot untouched
      }
    })();
    _workerStatusInflight = p;
    try {
      await p;
    } finally {
      _workerStatusInflight = null;
    }
  }

  /**
   * Kick off a sticky-worker preload for the currently selected engine,
   * then poll worker/status every 1s for up to 30s to flip the
   * `preloadState` dot from 'loading' → 'warm'.
   *
   * Multiple concurrent calls share the same poll token — the latest call
   * wins; older polls bail when they see the token has changed.
   */
  async function triggerPreload(): Promise<void> {
    const eng = VOICE_ENGINES.find((e) => e.id === _engineIdSingleton.value);
    const targetModelId = eng?.modelId;
    // If already warm, nothing to do.
    if (
      targetModelId !== undefined &&
      _loadedModelsSingleton.value.some((m) => m.modelId === targetModelId)
    ) {
      _preloadStateSingleton.value = "warm";
      return;
    }
    _preloadStateSingleton.value = "loading";
    const myToken = ++_workerPollToken;

    let status = "";
    try {
      // V1 parity (useVoiceInput.js:316-322): the preload is
      // parameter-driven — we POST the *currently selected* engine's
      // model/variant so picking Whisper warms Whisper and picking
      // Zipformer warms Zipformer (both reach "ready" independently since
      // the sticky worker is multi-model). The previous empty body made the
      // backend fall back to the single DB `preferred_model_id`, so the
      // non-preferred engine's dot spun forever.
      const r = await apiJson<PreloadResultResponse>(
        "POST",
        "/api/app-builder/voice-input/preload",
        {
          model_id: eng?.modelId ?? null,
          variant_id: eng?.variantId ?? null,
        },
      );
      status = r.status ?? "";
    } catch {
      _preloadStateSingleton.value = "failed";
      return;
    }
    if (myToken !== _workerPollToken) return; // superseded

    if (status === "warm" || status === "ready") {
      // Refresh once to populate loadedModels for the dots.
      await refreshWorkerStatus();
      _preloadStateSingleton.value = "warm";
      return;
    }
    if (status === "failed") {
      _preloadStateSingleton.value = "failed";
      return;
    }

    // status === "loading" → poll until the model becomes resident.
    // The backend warm-load budget is 120s (`StickyWorkerHost`
    // `LOAD_MODEL_TIMEOUT_S` + `warm_load_model_into_host`'s
    // `wait_for(..., 120.0)`), and a cold Whisper load (encoder+decoder QNN
    // contexts ~235MB, ~10-15s, longer if queued behind the NPU lock) easily
    // exceeds the old 30s window — which left the dot spinning forever even
    // after the backend finished loading. Mirror the backend budget (+grace)
    // so the dot reliably flips to "warm" once residency is confirmed.
    const deadline = Date.now() + PRELOAD_POLL_BUDGET_MS;
    while (Date.now() < deadline) {
      if (myToken !== _workerPollToken) return;
      await new Promise((resolve) => setTimeout(resolve, 1000));
      if (myToken !== _workerPollToken) return;
      await refreshWorkerStatus();
      if (
        targetModelId !== undefined &&
        _loadedModelsSingleton.value.some((m) => m.modelId === targetModelId)
      ) {
        _preloadStateSingleton.value = "warm";
        return;
      }
    }
    // Timed out — leave state at 'loading' so UI keeps spinning until the
    // user refreshes the popover or runs a transcription.
  }

  const loadedModels = _loadedModelsSingleton;
  const preloadState = _preloadStateSingleton;

  /**
   * `true` iff the currently selected engine's model is in the worker
   * `loaded_models` snapshot. Drives the green Ready dot in V1's pill /
   * popover.
   */
  const isCurrentEngineWarm = computed<boolean>(() => {
    const cur = engineId.value;
    const eng = VOICE_ENGINES.find((e) => e.id === cur);
    if (eng === undefined) return false;
    return loadedModels.value.some((m) => m.modelId === eng.modelId);
  });

  // ─── Recording handles ───────────────────────────────────────────────────

  let mediaStream: MediaStream | null = null;
  let audioCtx: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  let scriptProcessor: ScriptProcessorNode | null = null;
  let pcmBuffers: Float32Array[] = [];
  let srcSampleRate = TARGET_SAMPLE_RATE;
  let meterRaf = 0;
  let recordTimer: ReturnType<typeof setInterval> | null = null;
  /** Timer that fires every INTERIM_CHUNK_SECS to submit an interim chunk. */
  let interimTimer: ReturnType<typeof setInterval> | null = null;
  /** True while a transcribeChunk call is in-flight; prevents overlapping requests. */
  let interimInflight = false;
  /**
   * True once any interim chunk in the CURRENT recording session has
   * successfully delivered non-empty text. This is the persistent
   * "did interim ever produce something?" signal used by stop()'s
   * spurious-warning suppression.
   *
   * It MUST be a dedicated flag and NOT derived from
   * `partialTranscript.value`: stop() now clears `partialTranscript` to
   * "" the moment it enters "processing" (to prevent partial/final
   * double-emit), and the VoiceInputBtn consumer also resets it to ""
   * after forwarding each chunk. So by the time the tail-segment
   * inference completes, `partialTranscript.value` is always "" — using
   * it as the "interim succeeded" gate would re-introduce the spurious
   * "No audio captured" / "No speech recognized" toasts that commit
   * e8579f5 fixed.
   *
   * Reset to false in `resetIdle()` and at the top of `start()`.
   */
  let interimDeliveredAny = false;
  let cancelled = false;
  /** 每次 start() 递增，用于使旧录音的 transcribeChunk 结果失效 */
  let _sessionToken = 0;
  /** AbortController used to cancel the SSE leg of the current run. */
  let runAbort: AbortController | null = null;
  /** The currently in-flight run id (for explicit cancel). */
  let currentRunId: string | null = null;

  const maxDuration = opts.maxDurationSecs ?? 120;

  function cleanupRecording(): void {
    if (meterRaf !== 0) {
      cancelAnimationFrame(meterRaf);
      meterRaf = 0;
    }
    if (recordTimer !== null) {
      clearInterval(recordTimer);
      recordTimer = null;
    }
    if (interimTimer !== null) {
      clearInterval(interimTimer);
      interimTimer = null;
    }
    if (scriptProcessor !== null) {
      scriptProcessor.onaudioprocess = null;
      try {
        scriptProcessor.disconnect();
      } catch {
        // ignore
      }
      scriptProcessor = null;
    }
    if (audioCtx !== null) {
      void audioCtx.close().catch(() => undefined);
      audioCtx = null;
    }
    analyser = null;
    if (mediaStream !== null) {
      for (const tr of mediaStream.getTracks()) tr.stop();
      mediaStream = null;
    }
    meterLevel.value = 0;
  }

  function meterLoop(): void {
    if (analyser === null) return;
    const buf = new Uint8Array(analyser.fftSize);
    const tick = (): void => {
      if (analyser === null) return;
      analyser.getByteTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = ((buf[i] ?? 128) - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buf.length);
      meterLevel.value = Math.min(1, rms * 2);
      meterRaf = requestAnimationFrame(tick);
    };
    tick();
  }

  function setError(msg: string): void {
    errorText.value = msg;
    state.value = "error";
    // V1 parity (useVoiceInput.js `_toast('warning', msg)`): every
    // recording / transcription failure surfaces a VISIBLE toast, not
    // just a silent error state on the button. Without this the V2 mic
    // button looked like it "did nothing" on failure.
    toast.warning(msg);
  }

  /**
   * PR-F1 (F-15) — dispatch a runner-side failure to the right i18n
   * key. Mirrors V1 ``useVoiceInput.js`` (L744-775) but uses the V2
   * structured ``error_code`` carried by the SSE ``error`` frame's
   * ``details.error_code`` (or the REST DTO's ``error_code`` field on
   * the polling fallback). Behaviour:
   *
   *   * ``WEIGHTS_NOT_INSTALLED`` → ``voiceInput.weightsMissing`` (V1
   *     "guide user to AppBuilder" UX) with ``{model}`` interpolation.
   *   * ``AUDIO_DECODE_ERROR`` / ``INVALID_INPUT`` → ``voiceInput.encodeFailed``
   *     (per docs/30-ui-ux/voice-input-and-sticky-worker-multimodel.md
   *     §6.5 Table). The message tail is appended as detail.
   *   * Anything else (or ``null``) → ``voiceInput.inferenceFailed``
   *     followed by the raw message — keeps unknown server-side codes
   *     observable in the toast for diagnosis.
   *
   * The friendly key is fetched via vue-i18n; the locale files
   * (`frontend/src/locales/{zh-CN,en,zh-TW}/voiceInput.ts`) carry all
   * three keys in three languages — see §3.10 UTF-8 nothing to add.
   */
  function setInferenceError(
    code: string | null | undefined,
    detail: string,
  ): void {
    const eng = engine.value;
    let userMsg: string;
    if (code === "WEIGHTS_NOT_INSTALLED") {
      userMsg = t("voiceInput.weightsMissing", { model: eng.label });
    } else if (
      code === "AUDIO_DECODE_ERROR" ||
      code === "INVALID_INPUT"
    ) {
      const head = t("voiceInput.encodeFailed");
      userMsg = detail !== "" ? `${head}: ${detail}` : head;
    } else {
      const head = t("voiceInput.inferenceFailed");
      const tail =
        code !== null && code !== undefined && code !== ""
          ? ` [${code}]`
          : "";
      userMsg =
        detail !== "" ? `${head}${tail}: ${detail}` : `${head}${tail}`;
    }
    setError(userMsg);
  }

  /**
   * V1 parity (useVoiceInput.js:513-535): "soft" failures that happen
   * *before* recording starts (unsupported browser / mic permission
   * denied) show a warning toast and return the button to `idle` rather
   * than entering the sticky `error` state. The button stays clickable
   * so the user can retry after granting permission.
   */
  function warnAndIdle(msg: string): void {
    errorText.value = msg;
    state.value = "idle";
    toast.warning(msg);
  }

  function resetIdle(): void {
    // Bump the session token here too (not only in start()). cancel()
    // routes through resetIdle() without a subsequent start(); without
    // this bump an interim chunk still in-flight from the cancelled
    // session would keep the same token and — because resetIdle() also
    // clears `cancelled` back to false — could pass the final
    // `sessionToken === _sessionToken` guard and write stale text into
    // partialTranscript AFTER the user cancelled. Bumping the token here
    // invalidates any such chunk.
    _sessionToken++;
    state.value = "idle";
    runAbort = null;
    currentRunId = null;
    cancelled = false;
    interimInflight = false;
    interimDeliveredAny = false;
    partialTranscript.value = "";
    errorText.value = "";
  }

  // ─── Public actions ──────────────────────────────────────────────────────

  async function start(): Promise<void> {
    if (state.value !== "idle" && state.value !== "error") return;
    errorText.value = "";
    cancelled = false;
    interimDeliveredAny = false;
    _sessionToken++;
    const _myToken = _sessionToken;
    pcmBuffers = [];

    if (!available.value) {
      // V1 parity (useVoiceInput.js:513-521): unsupported / disabled →
      // warning toast + stay idle (button remains clickable for retry).
      // 2026-06-21: routed through vue-i18n (key ``voiceInput.disabled`` /
      // ``voiceInput.unsupported``) so zh-CN / zh-TW users see the
      // localised string. The disabled-copy points at the *toolbar engine
      // selector* (next to the mic button) — which is the actual enable
      // entry point — instead of the older "voice preferences" wording
      // that didn't correspond to any UI surface.
      warnAndIdle(
        browserSupported.value
          ? t("voiceInput.disabled")
          : t("voiceInput.unsupported"),
      );
      return;
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
      // V1 parity (useVoiceInput.js:527-535): full message with error
      // name + message, warning toast, back to idle.
      const e = err as { name?: string; message?: string };
      const detail = `${e.name ?? ""}${e.message ? `: ${e.message}` : ""}`;
      warnAndIdle(
        `Microphone permission denied. Please allow microphone access in your browser settings.${
          detail !== "" ? ` (${detail})` : ""
        }`,
      );
      return;
    }

    try {
      const Ctor = getAudioContextCtor();
      if (Ctor === undefined) {
        setError("AudioContext is not available in this browser.");
        cleanupRecording();
        return;
      }
      audioCtx = new Ctor();
      srcSampleRate = audioCtx.sampleRate;
      if (audioCtx.state === "suspended") {
        await audioCtx.resume().catch(() => undefined);
      }
      const source = audioCtx.createMediaStreamSource(mediaStream);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 256;
      source.connect(analyser);
      meterLoop();

      scriptProcessor = audioCtx.createScriptProcessor(4096, 1, 1);
      scriptProcessor.onaudioprocess = (e: AudioProcessingEvent): void => {
        const input = e.inputBuffer.getChannelData(0);
        pcmBuffers.push(new Float32Array(input));
      };
      source.connect(scriptProcessor);
      scriptProcessor.connect(audioCtx.destination);
    } catch (err) {
      const e = err as { message?: string };
      setError(`Failed to start microphone: ${e.message ?? String(err)}`);
      cleanupRecording();
      return;
    }

    recordSecs.value = 0;
    recordTimer = setInterval(() => {
      recordSecs.value += 1;
      if (recordSecs.value >= maxDuration) {
        void stop();
      }
    }, 1000);

    // Every INTERIM_CHUNK_SECS, snapshot the accumulated PCM, clear the
    // live buffer, and kick off a background transcription. The live
    // recording continues uninterrupted — the chunk upload/infer runs in
    // parallel without blocking the mic capture loop.
    interimTimer = setInterval(() => {
      if (cancelled || state.value !== "recording") return;
      // Skip this tick if the previous chunk is still being transcribed —
      // at 3 s intervals a slow ASR run could otherwise pile up requests.
      if (interimInflight) return;
      const chunkBuffers = pcmBuffers.splice(0); // atomic snapshot + clear
      const chunkSr = srcSampleRate;
      void transcribeChunk(chunkBuffers, chunkSr, _myToken);
    }, INTERIM_CHUNK_SECS * 1000);

    // Fire-and-forget preload so the model loads while user is speaking.
    void triggerPreload();

    state.value = "recording";
  }

  async function stop(): Promise<void> {
    if (state.value !== "recording") return;
    state.value = "processing";
    partialTranscript.value = "";  // 清空 partial，防止与 final 重叠

    // Snapshot buffers and release mic immediately.
    const buffers = pcmBuffers.splice(0); // 原子快照：切走数据，留下空数组
    const sr = srcSampleRate;
    // pcmBuffers 已是空数组（splice 就地清空），无需再赋值
    cleanupRecording();

    if (cancelled) {
      resetIdle();
      return;
    }

    // ── 1. encode WAV ─────────────────────────────────────────────────────
    let wavBlob: Blob | null;
    try {
      wavBlob = await pcmToTargetWav(buffers, sr);
    } catch (err) {
      setError(`Failed to encode audio: ${(err as Error).message}`);
      return;
    }
    // If the tail buffer is empty or near-silent (interim already consumed
    // the audio), skip the final inference pass entirely and finish cleanly.
    // A size threshold of 44 + 1600 bytes matches ~0.05 s of 16-kHz PCM16 —
    // anything shorter cannot contain intelligible speech.
    if (wavBlob === null || wavBlob.size <= 44 + 1600) {
      // Only show "no audio" if nothing was captured at all (no interim
      // chunks succeeded either). If an interim chunk already delivered
      // text the session was productive — just finish silently.
      //
      // Consult `interimDeliveredAny`, NOT `partialTranscript.value`:
      // stop() cleared partialTranscript to "" on entry (partial/final
      // double-emit guard) and the consumer clears it after each
      // forward, so the ref is always "" here.
      if (!interimDeliveredAny) {
        setError("No audio captured. Please check your microphone and try again.");
      } else {
        resetIdle();
      }
      return;
    }

    // ── 2. multipart upload ──────────────────────────────────────────────
    let audioPath: string;
    try {
      const fd = new FormData();
      const fname = `voice-${Date.now()}.wav`;
      fd.append("file", new File([wavBlob], fname, { type: "audio/wav" }), fname);
      const res = await apiUpload<UploadAudioResponse>(
        "/api/app-builder/upload/audio",
        fd,
      );
      audioPath = res.artifact?.path ?? "";
      if (audioPath === "") {
        throw new Error("upload response missing artifact.path");
      }
    } catch (err) {
      const msg =
        err instanceof ApiError ? err.message : (err as Error).message;
      setError(`Failed to upload audio: ${msg}`);
      return;
    }

    if (cancelled) {
      resetIdle();
      return;
    }

    // ── 3. create run ────────────────────────────────────────────────────
    const eng = engine.value;
    let run: RunResponse;
    try {
      run = await apiJson<RunResponse>(
        "POST",
        "/api/app-builder/runs",
        {
          model_id: eng.modelId,
          inputs: {
            audio: audioPath,
            variant_id: eng.variantId,
            params: eng.params,
          },
        },
      );
      currentRunId = run.id;
    } catch (err) {
      const msg =
        err instanceof ApiError ? err.message : (err as Error).message;
      setError(`Inference failed to start: ${msg}`);
      return;
    }

    // ── 4. SSE — wait for terminal state, harvest transcript ──────────────
    runAbort = new AbortController();
    let parsedText = "";
    let sawError = false;
    let errMsg = "";
    // PR-F1 (F-15) — structured failure code carried by either
    // (a) the SSE ``error`` frame's ``details.error_code`` (preferred,
    //     set by ``RunStreamBroadcaster.replay`` from ``Run.error_code``),
    // or (b) the REST fallback DTO's ``error_code`` field. Stays
    // ``null`` when no error was observed; passed to ``setInferenceError``
    // for i18n dispatch.
    let errCode: string | null = null;
    try {
      await apiWsStream(
        `/api/app-builder/runs/${encodeURIComponent(run.id)}/ws`,
        {
          onFrame: (data) => {
            const env = (data ?? {}) as RunFrameEnvelope;
            const payload = (env.payload ?? {}) as Record<string, unknown>;
            // Backend run frames carry result chunks; the most common
            // terminal shape mirrors V1's `result.output.fullText`.
            const output = payload["output"] as
              | Record<string, unknown>
              | undefined;
            if (output !== undefined) {
              const ft = output["fullText"];
              if (typeof ft === "string" && ft.trim() !== "") {
                parsedText = ft;
              }
            }
            const text = payload["text"];
            if (
              typeof text === "string" &&
              text.trim() !== "" &&
              parsedText === ""
            ) {
              parsedText = text;
            }
            // PR-F1 (F-15) — runner ``error`` events embedded inside a
            // frame's payload (process_runner.py:358-367). Capture
            // ``code`` so we can dispatch even if the SSE error frame
            // (terminator) arrives without ``details.error_code``
            // (older backend / race).
            if (
              errCode === null &&
              payload["event"] === "error" &&
              typeof payload["code"] === "string" &&
              (payload["code"] as string).trim() !== ""
            ) {
              errCode = payload["code"] as string;
              sawError = true;
              const m = payload["message"];
              if (typeof m === "string" && m.trim() !== "" && errMsg === "") {
                errMsg = m;
              }
            }
          },
          onState: (data) => {
            const env = (data ?? {}) as RunStateEnvelope;
            if (env.status === "failed") {
              sawError = true;
            }
          },
          onError: (err) => {
            console.error("[voice] stream onError:", err);
            sawError = true;
            errMsg = err.message;
            // PR-F1 (F-15) — SSE error frame carries the structured
            // runner code under ``details.error_code`` (set by
            // ``RunStreamBroadcaster``). The top-level ``code`` is the
            // QaiError envelope code (``app_builder.run_failed``), not
            // the runner-level failure code; we read the nested one
            // instead so the toast dispatch matches V1 behaviour.
            const details = err.details;
            if (details !== undefined) {
              const ec = details["error_code"];
              if (typeof ec === "string" && ec.trim() !== "") {
                errCode = ec;
              }
            }
          },
          onDone: () => {
          },
        },
        {
          signal: runAbort.signal,
          sseFallbackPath: `/api/app-builder/runs/${encodeURIComponent(run.id)}/stream`,
          sseOptions: { signal: runAbort.signal },
        },
      );
    } catch (err) {
      if (cancelled) {
        resetIdle();
        return;
      }
      const msg =
        err instanceof ApiError ? err.message : (err as Error).message;
      setError(`Inference stream failed: ${msg}`);
      return;
    }

    if (cancelled) {
      resetIdle();
      return;
    }

    // Fall-back: if the SSE channel ended without a transcript event,
    // poll `GET /runs/{id}` once — its `artifacts` list and status carry
    // the final answer for short, fast runs that completed before the
    // SSE replay window opened.
    if (cancelled) {
      resetIdle();
      return;
    }
    if (parsedText === "" && !sawError) {
      try {
        const final = await apiJson<RunResponse>(
          "GET",
          `/api/app-builder/runs/${encodeURIComponent(run.id)}`,
        );
        if (final.error_message !== null && final.error_message !== undefined) {
          sawError = true;
          errMsg = final.error_message;
          // PR-F1 (F-15) — append-only ``error_code`` on the REST DTO.
          if (
            errCode === null &&
            final.error_code !== null &&
            final.error_code !== undefined &&
            final.error_code !== ""
          ) {
            errCode = final.error_code;
          }
        }
      } catch {
        // ignore — we'll surface a generic error below
      }
    }

    if (sawError) {
      // PR-F1 (F-15) — dispatch on the structured ``error_code`` to the
      // matching i18n key (``voiceInput.weightsMissing`` /
      // ``voiceInput.encodeFailed`` / default ``voiceInput.inferenceFailed``).
      // V1 parity for the user-facing UX; previous behaviour hard-coded
      // the English fallback "Inference failed: please ensure the ASR
      // model weights are downloaded." regardless of locale.
      setInferenceError(errCode, errMsg);
      return;
    }

    if (parsedText === "") {
      // If interim chunks already delivered text, the tail segment simply
      // contained no additional speech (e.g. trailing silence after the
      // user stopped talking). Finish silently instead of showing an error.
      //
      // Consult `interimDeliveredAny`, NOT `partialTranscript.value`
      // (cleared on stop() entry + after every consumer forward).
      if (interimDeliveredAny) {
        resetIdle();
      } else {
        setError(
          "No speech recognized. Please try again with clearer audio, or ensure the selected ASR engine is downloaded.",
        );
      }
      return;
    }

    transcript.value = parsedText;
    void persistPreference(eng);
    resetIdle();
  }

  // ─── Interim chunk transcription (fires every INTERIM_CHUNK_SECS) ────────

  /**
   * Upload + transcribe a PCM snapshot taken during live recording.
   * Runs entirely in the background — errors are swallowed so a transient
   * network hiccup never kills the recording session.
   * On success, `partialTranscript` is updated; VoiceInputBtn watches it
   * and emits `transcribed` to append the text to the composer immediately.
   */
  async function transcribeChunk(
    chunkBuffers: Float32Array[],
    chunkSr: number,
    sessionToken: number,
  ): Promise<void> {
    if (cancelled || chunkBuffers.length === 0) return;
    interimInflight = true;
    try {
      await _transcribeChunkInner(chunkBuffers, chunkSr, sessionToken);
    } finally {
      interimInflight = false;
    }
  }

  async function _transcribeChunkInner(
    chunkBuffers: Float32Array[],
    chunkSr: number,
    sessionToken: number,
  ): Promise<void> {
    if (cancelled || chunkBuffers.length === 0) return;

    let wavBlob: Blob | null;
    try {
      wavBlob = await pcmToTargetWav(chunkBuffers, chunkSr);
    } catch {
      return; // encoding failure — skip silently
    }
    // Skip near-silent / too-short chunks (< ~0.1 s of actual audio after
    // WAV header). 44 bytes is the header-only size; add a small threshold.
    if (wavBlob === null || wavBlob.size <= 44 + 1600) return;

    let audioPath: string;
    try {
      const fd = new FormData();
      const fname = `voice-chunk-${Date.now()}.wav`;
      fd.append(
        "file",
        new File([wavBlob], fname, { type: "audio/wav" }),
        fname,
      );
      const res = await apiUpload<UploadAudioResponse>(
        "/api/app-builder/upload/audio",
        fd,
      );
      audioPath = res.artifact?.path ?? "";
      if (audioPath === "") return;
    } catch {
      return;
    }

    if (cancelled) return;

    const eng = engine.value;
    let run: RunResponse;
    try {
      run = await apiJson<RunResponse>("POST", "/api/app-builder/runs", {
        model_id: eng.modelId,
        inputs: {
          audio: audioPath,
          variant_id: eng.variantId,
          params: eng.params,
        },
      });
    } catch {
      return;
    }

    if (cancelled) return;

    let chunkText = "";
    const _chunkAbort = new AbortController();
    const _chunkTimeout = setTimeout(() => _chunkAbort.abort(), 10_000);
    try {
      await apiWsStream(
        `/api/app-builder/runs/${encodeURIComponent(run.id)}/ws`,
        {
          onFrame: (data) => {
            const env = (data ?? {}) as RunFrameEnvelope;
            const payload = (env.payload ?? {}) as Record<string, unknown>;
            const output = payload["output"] as
              | Record<string, unknown>
              | undefined;
            if (output !== undefined) {
              const ft = output["fullText"];
              if (typeof ft === "string" && ft.trim() !== "") {
                chunkText = ft;
              }
            }
            const text = payload["text"];
            if (
              typeof text === "string" &&
              text.trim() !== "" &&
              chunkText === ""
            ) {
              chunkText = text;
            }
          },
          onState: () => undefined,
          onError: () => undefined,
          onDone: () => undefined,
        },
        {
          signal: _chunkAbort.signal,
          sseFallbackPath: `/api/app-builder/runs/${encodeURIComponent(run.id)}/stream`,
          sseOptions: { signal: _chunkAbort.signal },
        },
      );
    } catch {
      return;
    } finally {
      clearTimeout(_chunkTimeout);
    }

    if (!cancelled && sessionToken === _sessionToken && chunkText.trim() !== "") {
      // Mark the session productive BEFORE writing partialTranscript, so
      // stop()'s spurious-warning suppression can rely on it even though
      // the consumer immediately clears partialTranscript after forwarding.
      interimDeliveredAny = true;
      partialTranscript.value = chunkText.trim();
    }
  }

  function cancel(): void {
    cancelled = true;
    if (state.value === "recording") {
      pcmBuffers = [];
      cleanupRecording();
      resetIdle();
      return;
    }
    if (state.value === "processing") {
      if (runAbort !== null) {
        try {
          runAbort.abort();
        } catch {
          // ignore
        }
      }
      const rid = currentRunId;
      if (rid !== null && rid !== "") {
        // Fire-and-forget — backend stops the runner.
        const url = `${apiBaseUrl()}/api/app-builder/runs/${encodeURIComponent(
          rid,
        )}`;
        const csrf = readCsrfCookie();
        const headers: Record<string, string> = {};
        if (csrf !== null) headers[QAI_CSRF_HEADER] = csrf;
        void fetch(url, {
          method: "DELETE",
          credentials: "include",
          headers,
        }).catch(() => undefined);
      }
      resetIdle();
    }
  }

  /** Toggle entry for the mic button. */
  function toggle(): void {
    if (state.value === "idle" || state.value === "error") {
      void start();
    } else if (state.value === "recording") {
      void stop();
    } else {
      cancel();
    }
  }

  onBeforeUnmount(() => {
    _workerPollToken++;
    cancel();
    cleanupRecording();
  });

  return {
    // state
    state,
    isListening,
    isProcessing,
    isBusy,
    recordSecs,
    meterLevel,
    transcript,
    partialTranscript,
    errorText,
    available,
    backendEnabled,
    browserSupported,
    // engine
    engineId,
    engine,
    setEngine,
    availableEngines: VOICE_ENGINES,
    refreshPreference,
    // worker / preload status (T2.7-D)
    loadedModels,
    preloadState,
    isCurrentEngineWarm,
    refreshWorkerStatus,
    triggerPreload,
    // actions
    start,
    stop,
    cancel,
    toggle,
  };
}
