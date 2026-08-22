<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * TranscriptPlayer — ASR output (Whisper / Zipformer), V1 `TranscriptPlayer.js`
 * behavior parity.
 *
 * V2 reimplementation in TS/Composition API. Output schema (field source of
 * truth: factory/chat_features/app-builder/models/{whisper-base,zipformer-zh}/manifest.json
 * `outputSchema.jsonSchema`):
 *
 *   segments[].start — start time in seconds
 *   segments[].end   — end time in seconds
 *   segments[].text  — recognized text
 *   segments[].conf  — confidence in [0,1] (Whisper only; Zipformer omits it)
 *   fullText         — all segment texts joined by a single space
 *   language         — detected/declared language (Whisper)
 *
 * The audio source is the run INPUT audio (`run.inputs.audio`, resolved by the
 * parent DynamicOutput), letting the user listen while reading — ASR output
 * itself carries no audio path. Clicking a segment seeks the (hidden) audio
 * element; `timeupdate` highlights the active segment.
 *
 * Uses the global `.ab-asr-*` classes from styles/app-builder/app-builder.css.
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";

/** ASR segment — matches the runner `segments[]` item schema. */
export interface TranscriptSegment {
  start: number;
  end: number;
  text: string;
  /** Confidence in [0,1]; absent for Zipformer (RNN-T greedy). */
  conf?: number;
}

interface Props {
  /** Time-aligned recognized segments. */
  segments: TranscriptSegment[];
  /** Resolved URL of the input audio (parent resolves run.inputs.audio). */
  audioUrl?: string;
  /** Full recognized text (segments joined by a space). */
  fullText?: string;
  /** Declared/detected language. */
  language?: string;
}

const props = withDefaults(defineProps<Props>(), {
  audioUrl: "",
  fullText: "",
  language: "",
});

const { t } = useI18n();

const hasAudio = computed<boolean>(() => props.audioUrl !== "");

// ── current time / active segment ──────────────────────────────────────────
const audioEl = ref<HTMLAudioElement | null>(null);
const currentTime = ref(0);
const audioLoadErr = ref(false);

function onTimeUpdate(e: Event): void {
  const el = e.target as HTMLAudioElement | null;
  currentTime.value = el?.currentTime ?? 0;
}
function onAudioError(): void {
  audioLoadErr.value = true;
}

const activeSegmentIdx = computed<number>(() => {
  const tNow = currentTime.value;
  const segs = props.segments;
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i];
    if (s !== undefined && tNow >= s.start && tNow <= s.end) return i;
  }
  return -1;
});

function jumpTo(seg: TranscriptSegment): void {
  const a = audioEl.value;
  if (a === null) return;
  try {
    a.currentTime = seg.start || 0;
    void a.play().catch(() => {});
  } catch {
    // ignore
  }
}

// ── time formatting (V1 `_fmtSec`) ─────────────────────────────────────────
function fmtSec(time: number): string {
  if (!Number.isFinite(time)) return "00:00";
  const sign = time < 0 ? "-" : "";
  const x = Math.abs(time);
  const m = Math.floor(x / 60);
  const s = Math.floor(x % 60);
  return `${sign}${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
</script>

<template>
  <div class="ab-asr">
    <!-- hidden audio element (used only to sync subtitle jumps; the input panel
         already shows a visible audio control) -->
    <audio
      v-if="hasAudio && !audioLoadErr"
      ref="audioEl"
      preload="metadata"
      :src="audioUrl"
      style="display: none"
      @timeupdate="onTimeUpdate"
      @error="onAudioError"
    ></audio>

    <!-- segment list -->
    <ul class="ab-asr-segments">
      <li
        v-for="(seg, i) in segments"
        :key="i"
        class="ab-asr-seg"
        :class="{ active: activeSegmentIdx === i }"
        style="cursor: pointer"
        @click="jumpTo(seg)"
      >
        <div class="ab-asr-seg-head">
          <span class="ab-asr-time">[{{ fmtSec(seg.start) }} – {{ fmtSec(seg.end) }}]</span>
          <span
            v-if="typeof seg.conf === 'number'"
            class="ab-asr-conf"
          >
            {{ (seg.conf * 100).toFixed(0) }}%
          </span>
        </div>
        <div class="ab-asr-text">
          {{ seg.text }}
        </div>
      </li>
      <li
        v-if="segments.length === 0"
        class="ab-asr-empty"
      >
        {{ t("appBuilder.asrEmpty") }}
      </li>
    </ul>

    <!-- full-text fallback (segments empty but fullText present) -->
    <div
      v-if="segments.length === 0 && fullText"
      class="ab-asr-fulltext"
    >
      {{ fullText }}
    </div>
  </div>
</template>
