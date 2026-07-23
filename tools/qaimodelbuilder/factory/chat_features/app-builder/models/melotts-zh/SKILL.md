# SKILL · MeloTTS Chinese (Full NPU — App Builder Pack)

> Injected when user selects `melotts-zh` Pack. Teaches how to *interpret*
> MeloTTS-zh output (audio + alignment), not how to call it.
>
> Both flows valid: you MAY call `appbuilder_run` to verify I/O shape;
> user's WebUI calls it over HTTP API. A Run result may already be in
> conversation — interpret it using this file.

---

## 1. What this Pack does

MeloTTS-zh: Chinese TTS from myshell-ai, fully on Snapdragon X Elite NPU (QNN HTP FP16). Four QNN context binaries, all NPU — no CPU vocoder:

1. **bert_wrapper** — BERT prosody/polyphone predictor
2. **encoder** — phoneme embeddings → hidden states
3. **flow** — normalizing flow (expressiveness)
4. **decoder** — T=128 HiFiGAN-class waveform decoder, flow_short dual-path

**Input:** Mandarin Chinese text string (ASCII digits auto-spelled-out; incidental English via G2P). Max 500 chars.

**Output:** JSON with `audio_path`, `duration_s`, `sample_rate` (44100 Hz), `alignment[]` (§3).

One voice (speaker_id=1, female), speed 0.5x–2.0x via `length_scale`. No multiple voices, emotion, cloning, SSML, multilingual, English-only, dialects, or singing (§6).

**Performance:** ~284 ms short sentences; ~1018 ms long (~5.6 s audio).

---

## 2. Parameters

| Param | Type | Default | Meaning |
|-------|------|---------|---------|
| `speed` | number 0.5–2.0, step 0.1 | `1.0` | Controls `length_scale`. `<1.0` lengthens phonemes (slower). `>1.0` shortens (faster). Usable across full range; below 0.6 sounds mechanically stretched, above 1.7 final consonants clip. **Speed scales alignment timestamps** — always in output (post-stretch) seconds. |

One voice (female, speaker_id=1), one sample rate (44100 Hz) — not configurable.

"Too fast/slow" → nudge `speed` ±0.2. Don't exceed 0.5–2.0 (clamped for quality).

---

## 3. Output JSON Schema (canonical contract)

```jsonc
{
  "audio_path": "data/outputs/<runId>.wav",  // repo-relative path; mono PCM-16
  "duration_s": 5.6,                          // seconds, float
  "sample_rate": 44100,                       // always 44100 Hz
  "alignment": [
    { "text": "今",      "start": 0.000, "end": 0.180 },
    { "text": "天",      "start": 0.180, "end": 0.350 },
    { "text": " ",       "start": 0.350, "end": 0.380 },
    { "text": "八",      "start": 0.380, "end": 0.560 },
    { "text": "点",      "start": 0.560, "end": 0.720 },
    { "text": "三",      "start": 0.720, "end": 0.880 },
    { "text": "十",      "start": 0.880, "end": 1.020 },
    { "text": "分",      "start": 1.020, "end": 1.200 },
    { "text": "，",      "start": 1.200, "end": 1.420 },
    ...
    { "text": "meeting", "start": 2.600, "end": 3.080 },
    ...
  ]
}
```

### 3.1 `audio_path`

Repo-relative WAV path (`data/outputs/<runId>.wav`). Always mono PCM-16 44100 Hz. No re-encoding (MP3/OGG/Opus) — WAV only.

### 3.2 `duration_s`

Float seconds = `len(waveform) / 44100`. Last alignment `end` within ~50 ms of this value.

### 3.3 `sample_rate`

Always 44100. Fixed. Echoed so consumers skip WAV header parsing.

### 3.4 `alignment[]`

Enables subtitle/karaoke/word-highlight features.

#### 3.4.1 Tokenization rule

Each entry is one of:
- Single **Chinese character**
- **Punctuation** (`，。？！；：`) — model-predicted pause (silent)
- **ASCII space** (~20–40 ms silent)
- **English word** kept whole (phonemes remapped to word level)
- **Spelled-out digit** — input `8` → `"text": "八"` (spoken form)

#### 3.4.2 Timestamp semantics

- `start`/`end` in seconds from audio start, post-`speed` time
- Sorted ascending, non-overlapping
- ±20 ms per-character precision at speed 1.0
- `start[0]` typically not 0.0 (50–150 ms warm-up breath)

---

## 4. Chinese frontend rewrites

### 4.1 Number-to-words (`cn2an`)

ASCII digits spelled out: `8` → `八`; `30` in `8点30分` → `三十`; `2026` → `两千零二十六` or `二零二六` (context-dependent).

### 4.2 Tone sandhi

Applied phonetically (3rd-tone, `一`/`不`, neutral-tone particles). Not in alignment, audible in output.

### 4.3 Polyphone disambiguation (BERT)

bert_wrapper resolves in context: `重要` → zhòng yào (not chóng); `银行` → yín háng (not xíng).

If wrong (rare — slang/classical): suggest synonym replacement or space insertion.

### 4.4 Embedded English

Small G2P (`g2p_en`). Common words (`meeting`, `email`, `OK`, `iPhone`) sound fine; rare jargon may mispronounce. **Not bilingual TTS** — mostly-English input needs a different model.

### 4.5 Length cap

>500 chars rejected. Split into chunks, concatenate WAVs client-side (re-offset alignment per chunk).

---

## 5. Typical user requests

### 5.1 "Read this text aloud"

Run already happened — WAV at `audio_path`. User clicks ▶ in UI.

### 5.2 "Slower / faster"

Suggest `speed=0.8` / `speed=1.2`. Requires **re-Run** (input param, not playback control). Pitch preserved.

### 5.3 "Export as SRT / VTT"

Walk `alignment[]`, new cue at each punctuation token. ~20 ms accuracy → 3-decimal precision is honest.

### 5.4 "Why 8 → 八?"

Number-to-words frontend (§4.1). Alignment shows spoken form for subtitle sync.

### 5.5 "Different / male voice"

**Not available.** One female voice only (speaker_id=1).

### 5.6 "Emotion / sad / cheerful"

**Not supported.** Fixed prosodic prior. Only speed is adjustable.

---

## 6. Known limitations

- **Single voice** — one female (speaker_id=1). No male, no selection, no cloning.
- **No emotion control** — fixed neutral style.
- **No SSML** — `<break>`, `<emphasis>`, `<phoneme>` read literally.
- **No dialects** — Cantonese/Wu/Min unsupported; Standard Mandarin only.
- **No English-only** — embedded G2P for incidental English only.
- **No singing** — TTS only.
- **No whisper/shout** — fixed loudness.
- **Emoji/pictographs** — skipped silently.
- **Very short input** (1–2 chars) — clipped prosody; pad with punctuation.
- **WAV only** — mono PCM-16 44100 Hz. No MP3/Opus/stereo/alt rates.
- **500-char limit** per Run.

Wrong pronunciation? Check: polyphone (§4.3) → digit (§4.1) → English G2P edge (§4.4) → dialect text read as Mandarin.

---

## 7. Architecture (full NPU)

All four models on Snapdragon X Elite NPU via QNN HTP FP16:

| Model | Role | Notes |
|-------|------|-------|
| bert_wrapper | Prosody + polyphone prediction | Context-aware BERT |
| encoder | Phoneme → latent | Text encoder |
| flow | Latent refinement | Normalizing flow |
| decoder | Latent → waveform (44100 Hz) | T=128, flow_short dual-path |

Real-time: ~284 ms short, ~1018 ms long (~5.6 s audio). No CPU fallback.

---

## 8. What you (the LLM) must NOT do

- **Don't re-run to interpret existing results.** If Run result is in context, interpret it. You MAY `appbuilder_run` to verify I/O when building WebUI.
- **Don't modify** Pack files (developer-maintained). MAY `read` `runner.py` READ-ONLY for I/O understanding. Run inference via HTTP API / `appbuilder_run` — never execute `runner.py` in generated app.
- **Don't invent fields** — only `audio_path`, `duration_s`, `sample_rate`, `alignment[].{text,start,end}`.
- **Don't promise** multiple voices, emotion, dialects, cloning, SSML, or English-only TTS.
- **Don't edit the audio.** Summarize, generate subtitles, recommend re-run with different speed — but no WAV editing.
- **Don't translate audio language.** Output is Mandarin. English audio → tell user this Pack can't.
- **Don't promise sub-character precision.** Alignment is per-character/word/digit.
- **Don't suggest sample rate changes.** Always 44100 Hz, no alternatives.

---

## 9. Quick reference — example output

Input `今天 8 点 30 分有个 meeting，请准时参加。` with `speed=1.0`:

```jsonc
{
  "audio_path":  "data/outputs/r-abc123.wav",
  "duration_s":  4.32,
  "sample_rate": 44100,
  "alignment": [
    { "text": "今",       "start": 0.080, "end": 0.260 },
    { "text": "天",       "start": 0.260, "end": 0.420 },
    { "text": " ",        "start": 0.420, "end": 0.450 },
    { "text": "八",       "start": 0.450, "end": 0.640 },
    { "text": "点",       "start": 0.640, "end": 0.810 },
    { "text": "三",       "start": 0.810, "end": 0.970 },
    { "text": "十",       "start": 0.970, "end": 1.120 },
    { "text": "分",       "start": 1.120, "end": 1.310 },
    "...",
    { "text": "meeting",  "start": 1.900, "end": 2.460 },
    { "text": "，",       "start": 2.460, "end": 2.700 },
    "...",
    { "text": "。",       "start": 3.620, "end": 4.320 }
  ]
}
```

Digits spelled out as Chinese; English words kept whole; punctuation tokens have non-zero spans (model pauses).
