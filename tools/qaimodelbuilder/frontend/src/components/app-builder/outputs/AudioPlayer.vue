<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * AudioPlayer — TTS output viewer with waveform visualization, playback
 * controls, alignment token highlighting, and seek-to-time.
 *
 * V1 parity: QAIModelBuilder_v1_pure/frontend/js/components/app-builder/outputs/AudioPlayer.js
 *
 * Features:
 *   - Play/Pause button + custom waveform (canvas "lightning bolt" style)
 *   - Click-to-seek on waveform
 *   - Current time / total duration display
 *   - Alignment tokens with active highlight (currentTime in [start,end])
 *   - Click token → jump to that timestamp
 *   - Animated waveform during playback (breathing + flow drift)
 *   - ResizeObserver for responsive canvas redraw
 *
 * Uses global `.ab-tts-*` classes from styles/app-builder/app-builder.css.
 */
import { ref, computed, watch, onMounted, onBeforeUnmount } from "vue";
import { useI18n } from "vue-i18n";

// ── Types ──────────────────────────────────────────────────────────────────

interface AlignmentToken {
  start: number;
  end: number;
  text: string;
}

interface AudioData {
  url: string;
  duration_seconds?: number;
  sample_rate?: number;
  format?: string;
  /** V1-style alignment tokens for text-audio sync highlighting. */
  alignment?: AlignmentToken[];
}

interface Props {
  data: AudioData;
  /** Optional external segments (alternative to data.alignment). */
  segments?: AlignmentToken[];
}

const props = withDefaults(defineProps<Props>(), {
  segments: undefined,
});

defineEmits<{
  download: [];
}>();

const { t } = useI18n();

// ── Derived data ───────────────────────────────────────────────────────────

const alignment = computed<AlignmentToken[]>(() => {
  const ext = props.segments;
  if (Array.isArray(ext) && ext.length > 0) return ext;
  const arr = props.data?.alignment;
  return Array.isArray(arr) ? arr : [];
});

const duration = computed<number>(
  () => Number(props.data?.duration_seconds) || 0,
);

const sampleRate = computed<number | null>(
  () => (typeof props.data?.sample_rate === "number" ? props.data.sample_rate : null),
);

const audioUrl = computed<string>(() => props.data?.url ?? "");

// ── Playback state ─────────────────────────────────────────────────────────

const audioEl = ref<HTMLAudioElement | null>(null);
const currentTime = ref(0);
const audioLoadErr = ref(false);
const isPlaying = ref(false);

function onTimeUpdate(e: Event): void {
  const el = e.target as HTMLAudioElement;
  currentTime.value = el.currentTime || 0;
}
function onAudioError(): void {
  audioLoadErr.value = true;
}
function onPlay(): void {
  isPlaying.value = true;
}
function onPause(): void {
  isPlaying.value = false;
}
function onEnded(): void {
  isPlaying.value = false;
}

function togglePlay(): void {
  const a = audioEl.value;
  if (!a) return;
  try {
    if (a.paused) void a.play().catch(() => {/* ignore */});
    else a.pause();
  } catch {
    // ignore
  }
}

// ── Waveform seek ──────────────────────────────────────────────────────────

const waveCanvas = ref<HTMLCanvasElement | null>(null);

function seekFromWave(e: MouseEvent): void {
  const a = audioEl.value;
  const cv = waveCanvas.value;
  if (!a || !cv || !duration.value) return;
  const rect = cv.getBoundingClientRect();
  const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  try {
    a.currentTime = pct * duration.value;
  } catch {
    // ignore
  }
}

// ── Alignment token highlight ──────────────────────────────────────────────

const activeTokenIdx = computed<number>(() => {
  const ct = currentTime.value;
  const arr = alignment.value;
  for (let i = 0; i < arr.length; i++) {
    const tok = arr[i];
    if (tok && ct >= tok.start && ct <= tok.end) return i;
  }
  return -1;
});

function jumpToToken(tok: AlignmentToken): void {
  const a = audioEl.value;
  if (!a) return;
  try {
    a.currentTime = tok.start || 0;
    void a.play().catch(() => {/* ignore */});
  } catch {
    // ignore
  }
}

// ── Lightning waveform canvas (V1 deterministic noise + animation) ─────────

const BOLT_POINTS = 96;

/** Deterministic pseudo-random amplitudes — stable across redraws. */
const boltAmps: Float32Array = (() => {
  const out = new Float32Array(BOLT_POINTS);
  let seed = 0x9e3779b1;
  for (let i = 0; i < BOLT_POINTS; i++) {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    const r = (seed & 0xffff) / 0xffff;
    const env = 0.5 + 0.5 * Math.sin((i / BOLT_POINTS) * Math.PI);
    const jag =
      (r - 0.5) * 1.6 +
      Math.sin(i * 0.83) * 0.3 +
      Math.sin(i * 0.31) * 0.45;
    out[i] = Math.max(-1, Math.min(1, jag * env));
  }
  return out;
})();

let animFrame = 0;
let animRaf: number | null = null;

function drawWaveform(): void {
  const cv = waveCanvas.value;
  if (!cv) return;
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const w = cv.clientWidth || 600;
  const h = cv.clientHeight || 60;
  cv.width = Math.floor(w * dpr);
  cv.height = Math.floor(h * dpr);
  const ctx = cv.getContext("2d");
  if (!ctx) return;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const cy = h / 2;
  const playPct =
    duration.value > 0 ? Math.min(1, currentTime.value / duration.value) : 0;
  const playX = w * playPct;

  // Color palette (V1 parity: teal future + purple past)
  const COLOR_FUTURE_CORE = "#7dd3fc";
  const COLOR_FUTURE_GLOW = "rgba(125,211,252,0.35)";
  const COLOR_PAST_CORE = "#c4b5fd";
  const COLOR_PAST_GLOW = "rgba(168,85,247,0.55)";
  const COLOR_BASELINE = "rgba(125,211,252,0.18)";

  // 1. Center baseline
  ctx.strokeStyle = COLOR_BASELINE;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, cy);
  ctx.lineTo(w, cy);
  ctx.stroke();

  // 2. Lightning bolt path
  const halfH = h * 0.42;
  const playingNow = isPlaying.value;
  const breathe = 0.85 + 0.15 * Math.sin(animFrame * 0.04);
  const intensity = playingNow ? 1.0 : 0.65;

  function drawPath(
    strokeStyle: string,
    lineWidth: number,
    blur: number,
  ): void {
    ctx!.shadowBlur = blur;
    ctx!.shadowColor = strokeStyle;
    ctx!.strokeStyle = strokeStyle;
    ctx!.lineWidth = lineWidth;
    ctx!.lineCap = "round";
    ctx!.lineJoin = "round";
    ctx!.beginPath();
    for (let i = 0; i < BOLT_POINTS; i++) {
      const x = (i / (BOLT_POINTS - 1)) * w;
      const amp = boltAmps[i] ?? 0;
      const drift = playingNow
        ? Math.sin(animFrame * 0.05 + i * 0.4) * 0.08
        : 0;
      const y = cy - (amp + drift) * halfH * intensity * breathe;
      if (i === 0) ctx!.moveTo(x, y);
      else ctx!.lineTo(x, y);
    }
    ctx!.stroke();
    ctx!.shadowBlur = 0;
  }

  // Future (right of playhead)
  ctx.save();
  ctx.beginPath();
  ctx.rect(playX, 0, w - playX, h);
  ctx.clip();
  drawPath(COLOR_FUTURE_GLOW, 4, 12);
  drawPath(COLOR_FUTURE_CORE, 1.4, 6);
  ctx.restore();

  // Past (left of playhead)
  if (playX > 0) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, playX, h);
    ctx.clip();
    drawPath(COLOR_PAST_GLOW, 5, 18);
    drawPath(COLOR_PAST_CORE, 1.6, 8);
    ctx.restore();

    // Playhead vertical light column
    const grad = ctx.createLinearGradient(playX, 0, playX, h);
    grad.addColorStop(0, "rgba(255,255,255,0)");
    grad.addColorStop(0.5, "rgba(255,255,255,0.95)");
    grad.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = grad;
    ctx.fillRect(playX - 1, 0, 2, h);
    // Top/bottom glow dots
    ctx.fillStyle = "#fff";
    ctx.beginPath();
    ctx.arc(playX, 2, 1.5, 0, Math.PI * 2);
    ctx.arc(playX, h - 2, 1.5, 0, Math.PI * 2);
    ctx.fill();
  }

  // 3. Subtle bottom reflection
  const reflGrad = ctx.createLinearGradient(0, cy, 0, h);
  reflGrad.addColorStop(0, "rgba(125,211,252,0.05)");
  reflGrad.addColorStop(1, "rgba(125,211,252,0)");
  ctx.fillStyle = reflGrad;
  ctx.fillRect(0, cy, w, h - cy);
}

function animLoop(): void {
  animFrame++;
  drawWaveform();
  if (isPlaying.value) {
    animRaf = requestAnimationFrame(animLoop);
  } else {
    animRaf = null;
  }
}

function startAnim(): void {
  if (animRaf != null) return;
  animRaf = requestAnimationFrame(animLoop);
}
function stopAnim(): void {
  if (animRaf != null) {
    cancelAnimationFrame(animRaf);
    animRaf = null;
  }
}

// ── Lifecycle & watchers ───────────────────────────────────────────────────

let resizeObserver: ResizeObserver | null = null;

onMounted(() => {
  drawWaveform();
  const cv = waveCanvas.value;
  if (cv && typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver(() => drawWaveform());
    resizeObserver.observe(cv);
  }
});

onBeforeUnmount(() => {
  stopAnim();
  if (resizeObserver) {
    resizeObserver.disconnect();
    resizeObserver = null;
  }
});

watch([currentTime, duration, audioUrl], () => {
  if (!isPlaying.value) drawWaveform();
});

watch(isPlaying, (playing) => {
  if (playing) startAnim();
  else {
    stopAnim();
    drawWaveform(); // final static frame
  }
});

// ── Time formatting ────────────────────────────────────────────────────────

function fmtSec(seconds: number): string {
  if (!Number.isFinite(seconds)) return "00:00";
  const x = Math.max(0, seconds);
  const m = Math.floor(x / 60);
  const s = Math.floor(x % 60);
  if (m === 0 && s === 0 && x > 0) return "00:01";
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
</script>

<template>
  <div class="ab-tts">
    <!-- Compact unified transport: [play] [waveform/scrubber] [time] -->
    <div
      v-if="audioUrl"
      class="ab-tts-bar"
    >
      <audio
        ref="audioEl"
        preload="metadata"
        :src="audioUrl"
        @timeupdate="onTimeUpdate"
        @error="onAudioError"
        @play="onPlay"
        @pause="onPause"
        @ended="onEnded"
      ></audio>
      <button
        type="button"
        class="ab-tts-play"
        :class="{ playing: isPlaying }"
        :aria-label="isPlaying ? t('appBuilder.audioPause', 'Pause') : t('appBuilder.audioPlay', 'Play')"
        @click="togglePlay"
      >
        <svg
          v-if="!isPlaying"
          viewBox="0 0 16 16"
          width="14"
          height="14"
          aria-hidden="true"
        >
          <path
            d="M4 2.5v11l10-5.5z"
            fill="currentColor"
          />
        </svg>
        <svg
          v-else
          viewBox="0 0 16 16"
          width="14"
          height="14"
          aria-hidden="true"
        >
          <rect
            x="3.5"
            y="2.5"
            width="3"
            height="11"
            rx="0.6"
            fill="currentColor"
          />
          <rect
            x="9.5"
            y="2.5"
            width="3"
            height="11"
            rx="0.6"
            fill="currentColor"
          />
        </svg>
      </button>
      <div
        class="ab-tts-wave-wrap"
        @click="seekFromWave"
      >
        <canvas
          ref="waveCanvas"
          class="ab-tts-wave-canvas"
        ></canvas>
      </div>
      <div class="ab-tts-time">
        {{ fmtSec(currentTime) }} / {{ fmtSec(duration) }}
        <span
          v-if="sampleRate"
          class="ab-tts-sr"
        > &middot; {{ sampleRate }}Hz</span>
      </div>
    </div>
    <div
      v-else
      class="ab-tts-empty"
    >
      {{ t("appBuilder.ttsNoAudio", "No audio output") }}
    </div>
    <div
      v-if="audioLoadErr"
      class="ab-tts-err"
    >
      {{ t("appBuilder.audioLoadError", "Failed to load audio") }}: {{ audioUrl }}
    </div>

    <!-- Alignment tokens (V1 parity: click to jump, active highlight) -->
    <div
      v-if="alignment.length"
      class="ab-tts-alignment"
    >
      <span
        v-for="(tok, i) in alignment"
        :key="i"
        class="ab-tts-token"
        :class="{ active: activeTokenIdx === i }"
        :title="fmtSec(tok.start) + ' \u2013 ' + fmtSec(tok.end)"
        @click="jumpToToken(tok)"
      >{{ tok.text }}</span>
    </div>
  </div>
</template>
