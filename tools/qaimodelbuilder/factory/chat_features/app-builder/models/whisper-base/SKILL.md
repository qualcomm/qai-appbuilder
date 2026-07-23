# SKILL · Whisper Base (App Builder Pack)

> Injected when user selects `whisper-base` Pack in App Builder and starts a chat.
> Teaches how to *interpret* Whisper-base output, not how to *call* it.
> Pack runs on-device. You MAY call `appbuilder_run` to verify I/O shape;
> user's WebUI calls it over HTTP API. A Run result may already be in context.

---

## 1. What this Pack does

Whisper-base: 74M-param encoder/decoder ASR (OpenAI), on Snapdragon QNN HTP, INT8.
Accepts one audio clip (WAV/MP3/FLAC/WEBM, mono 16 kHz, ≤120 s) → structured JSON with detected language, task, full text, and time-aligned segments.

**Input:** one audio file. Mic capture allowed (`allowMic=true`; front end records WAV 16 kHz mono).

**Output:** JSON with `language`, `task`, `fullText`, `segments[]` (schema in §3).

Two modes via `task` parameter:
- `transcribe` (default) — text in **source** language. Model auto-detects language (or uses `params.language` if forced). Chinese clip → Chinese text, English → English.
- `translate` — **English** output regardless of source. One-way only: any of ~99 languages **→ English**. Reverse direction not supported; user needs separate text-translation.

---

## 2. Parameters

| Param | Type | Default | Meaning |
|-------|------|---------|---------|
| `language` | select `auto`/`zh`/`en`/`ja`/`ko`/`fr`/`de`/`es`/`ru` | `auto` | Forces source language. `auto` uses Whisper's LID head — best for unknown clips but can mis-fire on short/noisy audio. Forcing skips LID, is faster + more reliable for `task=translate`. |
| `task` | select `transcribe`/`translate` | `transcribe` | See §1. `translate` → English only. For English input, `translate` is a no-op. |
| `vad` | boolean | `true` | Splits long audio into ≤30 s speech windows, skips silence. Disable only for clips ≤30 s of continuous speech. For ≥30 s audio, VAD is mandatory — without it only the first 30 s is transcribed. |
| `beam_size` | number 1–10 | `5` | Decoder beam width. 1=greedy (faster, more hallucinations/loops). 8–10 reduces hallucinations, slightly better WER, linearly slower. 5 is standard default. |

**Troubleshooting:**
- "Transcript repeats same phrase" → increase `beam_size` to 8–10 (repetition = greedy-decode failure).
- "Result in wrong language" → `language=auto` mis-detected (common on <5 s or music-heavy clips); force `language=<actual>`.

---

## 3. Output JSON Schema

```jsonc
{
  "language": "zh",                     // ISO 639-1 code of the source audio
  "task": "transcribe" | "translate",   // mirrors params.task
  "fullText": "segment0 text segment1 text ...",
  "segments": [
    {
      "start": 0.00,                    // seconds, from start of input audio
      "end": 3.42,                      // seconds, from start of input audio
      "text": "...",                    // recognized / translated text for this segment
      "conf": 0.91                      // average decoder log-prob mapped to [0,1]
    },
    ...
  ]
}
```

### 3.1 `language` field

- 2-letter ISO 639-1 code. Mandarin = `zh` (no `zh-CN`/`zh-TW` distinction).
- When `params.language != "auto"`, equals `params.language`. When `auto`, reflects Whisper's LID prediction — single guess for the **whole** clip, not per-segment. Code-switched audio shows **dominant** language; segments may contain words from others.
- Treat as a hint, not a hard guarantee.

### 3.2 `task` field

Always equals `params.task`. Included so downstream consumers can branch without re-reading the request.

### 3.3 `segments[]` timestamp precision

Timestamps are **second-level**, not frame-level. Decoder emits at 20 ms grid but end-to-end accuracy is **±0.3–0.5 s** due to:
- 30 s encoder window quantization (±0.5 s shift at boundaries after stitching)
- VAD chunk boundaries snapping to silence, not word edges
- Decoder hallucinations causing timestamp drift on noisy audio

Key facts:
- `start`/`end` are **absolute seconds from input start** (runner already added chunk offset)
- Sorted ascending, **non-overlapping** normally. Tiny overlaps (<50 ms) possible at chunk boundaries — clip client-side if needed.
- Typical segment: 2–10 s. No per-word timestamps in this Pack — word-level alignment requires a separate forced-aligner (e.g. WhisperX).

For SRT/VTT output, round to nearest 100 ms:

```
1
00:00:00,000 --> 00:00:03,420
First segment text

2
00:00:03,420 --> 00:00:07,800
Second segment text
```

The `,XXX` millisecond slot is format-required, not model-accurate.

### 3.4 `text` field — language and content

- `task=transcribe`: text in source language. Punctuation/casing are model-generated:
  - Chinese: full-width (`，` `。` `？` `！`), no spaces between characters.
  - English: standard ASCII punctuation, sentence-cased.
  - Japanese: mixed kana/kanji, no spaces.
- `task=translate`: **English** regardless of `language`, standard English punctuation.
- Non-speech annotations (`[Music]`, `(applause)`, `(笑い)`) are real model outputs. Strip with regex for pure speech.
- Empty segments filtered by runner; you won't see them.

### 3.5 `conf` field

- Range `[0,1]`. Computed as `exp(avg_logprob)` clipped, averaged over segment tokens.
- Good reads: 0.85–0.97. Below 0.6 = likely hallucination on silence/music/unclear speech.
- **Not** calibrated probability; use as **relative** quality signal within a single Run only.

### 3.6 `fullText` field

All segment `text`s joined with **a single space** (not `\n`). Use for prose output; for time-aligned use cases iterate `segments[]`.

---

## 4. Typical user requests

### 4.1 "Meeting summary"

1. Read `fullText`. Use `language` to pick summary language (match source unless user says otherwise).
2. Use `segments[]` for topic shifts: gaps `segments[i+1].start − segments[i].end > 5 s` = section breaks.
3. Structure:
   - **Participants** — Whisper does NOT do diarization. State: "I can identify topics/timestamps but not speakers."
   - **Key points** — bullets with `[mm:ss]` timestamps from source segments.
   - **Open questions** — unresolved items ("??", "TBD", "回头讨论", etc.).
4. Keep original language unless asked to translate.

### 4.2 "SRT / VTT subtitles"

Walk `segments[]` per §3.3 format. Notes:
- **VTT**: separator `.` (`00:00:00.000 --> 00:00:03.420`), header `WEBVTT`.
- **SRT**: 1-based index, separator `,`.
- If `task=="translate"`, subtitles are English even if `language` is `zh`/`ja` — inform user.
- Long segments (>7 s): for "broadcast-quality" mention splitting at sentence boundaries (`。`/`.`/`!`/`?`) with **estimated** timestamps (linear interpolation).

### 4.3 "Key decision points"

- Search `segments[]` for decision cues:
  - en: `decide(d)`, `agree(d)`, `we'll`, `let's`, `action item`, `commit to`, `by Friday`
  - zh: `决定`, `同意`, `我们就`, `下周之前`, `负责`, `跟进`
- Return `[mm:ss] text` (use `HH:MM:SS` for clips ≥1 h, else `MM:SS`).
- Group by topic if >5 matches; otherwise flat list.
- Always quote `text` verbatim — user wants verifiable timestamp pointers.

### 4.4 "Translate to English" (non-English source)

- If Run was `task=transcribe`, you have source-language `fullText`; downstream text-translation works.
- For best quality, suggest **re-Run** with `task=translate` — Whisper's joint audio-translation path beats two-step (transcribe→translate), especially for proper nouns/idioms.
- If source is already English (`language=="en"`), point that out instead of re-Running.

### 4.5 "Translate to Japanese / Chinese / German"

- Whisper-base supports translate-to-English **only**. State clearly. Workflow:
  1. Run `task=translate` → English text, OR
  2. Run `task=transcribe` → source-language text.
  3. Pipe through a separate text-translation tool (not included in this Pack).
- Don't pretend non-English translation targets work via post-processing.

---

## 5. Known limitations

Whisper-base (74M params) quality drops on:

- **Noisy/far-field audio** — background music, simultaneous speakers, >2 m from mic. Failures: hallucinated text on silence, repetition loops, wrong LID. Fix: re-record closer or denoise first.
- **Audio >120 s** — input capped at 120 s. Split client-side before uploading; concatenating `fullText` across runs works but timestamps reset per chunk (user must re-offset).
- **Heavy code-switching** — language changes every few seconds confuse LID. Force `language=<dominant>`; expect some transliteration of minority-language words.
- **Specialized vocabulary** — medical/legal/technical jargon poorly covered; base model guesses phonetically similar common words. Check `conf` for suspicious lines.
- **Speaker diarization** — **not supported.** No `speaker` field. Say so honestly.
- **Word-level timestamps** — **not supported.** Segment-level only.
- **Singing/lyrics** — unreliable; treat as best-effort.
- **Languages outside Whisper's 99** — model falls back to related language (often English), produces garbage. Pack limits `params.language` to 8 common languages + `auto`; less common may work via `auto` but is untested.

"Wrong text" reports → check `conf` and segment duration: short segments (<1 s) + conf <0.6 are usual culprits.

---

## 6. Whisper-base vs. zipformer-zh

| Dimension | `whisper-base` | `zipformer-zh` |
|-----------|----------------|-----------------|
| Languages | ~99 (multilingual) | Mandarin Chinese only |
| Translate-to-English | ✅ via `task=translate` | ❌ |
| Speed | ~6 s for 30 s audio (HTP, beam=5) | ~3 s for 30 s audio |
| Quality on clean Mandarin | Good | Slightly better, plus better with hotwords |
| Quality on code-switched zh+en | OK (multilingual) | Poor (English words butchered) |
| Hotword bias | ❌ | ✅ |
| Long-audio support | Same (VAD chunking ≤120 s) | Same |

Recommendation:
- Clean Mandarin + speed matters → **zipformer-zh**
- Non-Chinese languages present → **whisper-base**
- English translation needed → **whisper-base** with `task=translate`
- Domain hotwords (terms, names) → **zipformer-zh** with `hotwords` param
- Unsure → **whisper-base** (safer default)

Don't volunteer this comparison unless user is choosing between the two.

---

## 7. What you (the LLM) should NOT do

- **Don't re-run to interpret an existing result.** If a Run result is in context, interpret it. You MAY call `appbuilder_run` to verify I/O when building a WebUI, but re-running to change params is the user's job.
- **Do NOT MODIFY** developer-maintained files. You MAY `read` `runner.py` READ-ONLY for I/O understanding. Run inference via HTTP API / `appbuilder_run` — never execute `runner.py` inside generated app.
- ❌ **Don't invent fields** not in schema (no `speaker`, `words`, `confidence_avg`, `language_per_segment`). Only: `language`, `task`, `fullText`, `segments[].{start,end,text,conf}`.
- ❌ **Don't silently "fix" text.** If transcript says "我们用 React" and you suspect "react.js", **suggest** correction but flag as guess and quote original `text` + `[start–end]`.
- ❌ **Don't promise word-level timestamps or speaker labels** — not in output.
- ❌ **Don't translate when user asked for transcription** (or vice versa). Check `task` field; if user wants the other, tell them to re-Run with flipped `task` (prefer re-Run over post-hoc translation for quality).

---

## 8. Quick reference — example output

30 s Chinese news clip, `task=transcribe`, `language=auto`:

```json
{
  "language": "zh",
  "task": "transcribe",
  "fullText": "今天的天气非常好。 我们去公园散步吧。 顺便买点水果回来。",
  "segments": [
    { "start": 0.00,  "end": 3.42,  "text": "今天的天气非常好。",       "conf": 0.94 },
    { "start": 3.42,  "end": 7.80,  "text": "我们去公园散步吧。",       "conf": 0.91 },
    { "start": 7.80,  "end": 12.10, "text": "顺便买点水果回来。",       "conf": 0.89 }
  ]
}
```

Same audio with `task=translate`:

```json
{
  "language": "zh",
  "task": "translate",
  "fullText": "The weather is great today. Let's go for a walk in the park. We can pick up some fruit on the way back.",
  "segments": [
    { "start": 0.00,  "end": 3.42,  "text": "The weather is great today.",                       "conf": 0.92 },
    { "start": 3.42,  "end": 7.80,  "text": "Let's go for a walk in the park.",                  "conf": 0.90 },
    { "start": 7.80,  "end": 12.10, "text": "We can pick up some fruit on the way back.",        "conf": 0.88 }
  ]
}
```

`language` stays `zh` in `translate` — it describes **source** audio, not output text. Output language is implied: `transcribe` ⇒ source language, `translate` ⇒ English.
