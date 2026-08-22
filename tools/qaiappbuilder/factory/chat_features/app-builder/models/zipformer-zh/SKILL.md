# SKILL · Zipformer (Chinese) (App Builder Pack)

> Audience: this file is injected when the user selects `zipformer-zh`
> in App Builder and starts an LLM conversation. It teaches the model
> how to *interpret* zipformer-zh output, not how to *call* it.
>
> This Pack runs on-device. Both flows are valid: you MAY call
> `appbuilder_run` to verify I/O shape, and the user's WebUI calls it
> over HTTP API. A Run result may already be in conversation (after
> **Send to Chat**).

---

## 1. What this Pack does (one-liner)

Streaming RNN-T (k2-fsa / sherpa-onnx) for Mandarin Chinese on Snapdragon QNN HTP with INT8 quantization. Three QNN binaries: `encoder.bin` (streaming Zipformer encoder), `decoder.bin` (prediction network), `joiner.bin` (combines encoder embedding × decoder state → token distribution). Accepts one audio clip (WAV/WEBM, mono 16 kHz, ≤120 s), returns JSON with full text + time-aligned segments.

**Input:** one audio file. Mic capture allowed (`allowMic=true`); front end records WAV @ 16 kHz mono.

**Output:** JSON with `fullText`, `segments[]` (see schema below).

Output language is **always Mandarin Chinese (Simplified)** — no `language` field, no `task` parameter. For translation/English output → switch to `whisper-base` (§6).

---

## 2. Parameters (what the user can tune before Running)

| Param | Type | Default | Meaning |
|-------|------|---------|---------|
| `language` | select `zh` | `zh` | Fixed at `zh`. Present for forward compatibility only; today Mandarin-only with single option. |
| `vad` | boolean | `true` | Lightweight voice-activity-detection to strip silence and bound memory. The streaming RNN-T accepts arbitrary-length input; VAD is for memory bounds/silence skipping. With `vad=false` long silences may produce hallucinated tokens. |
| `hotwords` | text (multiline, advanced) | `""` | Newline-separated bias words. One word/phrase per line, no quotes/commas. Decoder score-boosts matching sequences for proper nouns/technical terms. Soft cap ~50; empty disables biasing. |

"Company/project name comes out wrong" → add to `hotwords`, re-Run.
"Garbage during silence" → check if `vad=false`, suggest re-enabling.

---

## 3. Output JSON Schema (canonical contract)

```jsonc
{
  "fullText": "segment0 text segment1 text ...",
  "segments": [
    {
      "start": 0.00,                    // seconds, from start of input audio
      "end": 3.42,                      // seconds, from start of input audio
      "text": "..."                     // recognized Mandarin text for this segment
    },
    ...
  ]
}
```

### 3.1 No `language` field

Output has **no** `language` field — language is implicitly Mandarin Chinese (`zh`, Simplified). Don't invent one. If downstream needs a language tag, hard-code `"zh"` from Pack identity.

### 3.2 No `task` field

No `task` parameter, no `task` output field. Zipformer-zh **only transcribes** — cannot translate. For translation → `whisper-base` with `task=translate` (§6).

### 3.3 No `conf` field — and why

Most-likely-to-trip-up-LLMs difference vs. whisper-base.

Whisper's softmax-over-vocabulary decoder yields per-token log-probs averaged into per-segment `conf`. RNN-T with **greedy** decoding does **not** expose comparable probabilities:

- Joiner softmax is over `vocab + {blank}`; per-frame decision is "emit non-blank?" not "how sure?" — most frames emit blank.
- Aggregating non-blank-frame probs tracks **frame rate / blank ratio** more than correctness — misleading as `conf`.
- Calibrated RNN-T confidence needs a CEM head this model lacks.

**LLM rules:** do **not** speculate about confidence, invent a `conf` field, or suggest "filter low-confidence segments". If asked: this model has no per-segment confidence. To detect errors: re-Run with `vad=true`, check audio intelligibility, or cross-check with `whisper-base` (which has `conf`).

### 3.4 `segments[]` time-stamp precision

Per-frame decisions on a 40 ms grid (downsampled from 10 ms frame shift). Runner converts to absolute seconds:

- `start`/`end` are **absolute seconds from input audio start** (per-chunk offset already applied).
- Segment boundary: silence ≥ ~0.5 s OR sentence-final punctuation. Typical duration 1–8 s.
- `segments[]` sorted by `start` ascending, non-overlapping.
- Real-world accuracy **±0.2–0.4 s** — better than whisper-base on chunk-boundary axis (no 30 s window), worse on absolute frame alignment (40 ms vs. 20 ms grid).

For SRT/VTT, round to nearest 100 ms:

```
1
00:00:00,000 --> 00:00:03,400
First segment text

2
00:00:03,400 --> 00:00:07,200
Second segment text
```

The `,XXX` millisecond slot is SRT format requirement, not model precision.

### 3.5 `text` field — Chinese-specific notes

- Always Simplified Mandarin. Traditional characters never appear even with Taiwanese pronunciation.
- Punctuation: full-width (`，` `。` `？` `！` `：` `；`), emitted during decoding (not post-processing). Quality OK but expect occasional missing periods.
- No spaces between Chinese characters.
- **Code-switched audio** (zh+en): English words get **transliterated into Chinese characters** ("锐艾克特" for React) or phonetic mush. Model's #1 weakness — suggest `whisper-base` for non-trivial English content (§5, §6).
- Numbers: spoken-out → Chinese numerals (`三百二十`); digit-by-digit → Arabic (`320`). Inconsistent.
- Empty segments (`text == ""`) filtered by runner.

### 3.6 `fullText` field

All segment `text`s concatenated with **a single space** between segments. Space preserved (despite Chinese not using spaces) so downstream can split for segment-level chunks. For prose output to Chinese readers, replace space with empty string; for time-aligned use cases iterate `segments[]`.

---

## 4. Typical user requests and how to handle them

### 4.1 "Summarize the transcript"

1. Read `fullText`. Output summary in **Chinese** unless user asks otherwise.
2. Use `segments[]` time anchors for topic shifts: gaps (`segments[i+1].start − segments[i].end > 5 s`) = section breaks.
3. Structure:
   - **Key topics** — bullets with `[mm:ss]` timestamps.
   - **Decisions** — cues: `决定`, `同意`, `就这么定`, `下周之前`.
   - **Open items** — cues: `回头讨论`, `再说`, `等确认`, `??`.
4. Quote original text + timestamp for verification.
5. **No speaker labels** — zipformer-zh has no diarization. State honestly if asked.

### 4.2 "Output as SRT subtitles"

Walk `segments[]` per §3.4 format:
- **SRT**: 1-based index, `,` time separator (`00:00:00,000 --> 00:00:03,400`).
- **VTT**: `.` separator + `WEBVTT` header.
- Long segments (>7 s): can heuristically split at `。`/`？`/`！` but split timestamps are **estimated** (linear interpolation on char count).
- Output is always Mandarin — if user expected English subtitles → redirect to `whisper-base` + `task=translate`.

### 4.3 "Translate the same audio with Whisper"

Redirect request. User wants to switch Pack to `whisper-base` with `task=translate`.

You **cannot** fulfill this by translating the transcript yourself — Whisper's joint audio-embedding translation is higher quality (especially proper nouns).

Tell the user:
> Switch model from `zipformer-zh` to `whisper-base` in the App Builder model selector, set `task` to `translate`, click Run. Same audio → English translation + time-stamped segments.

If user wants a rough English version now without re-Running, you may translate `fullText` but **flag it as LLM translation, not Whisper's native translation** with quality caveat.

### 4.4 "Find the key decisions"

- Search `segments[]` for decision cues: `决定`, `同意`, `我们就`, `下周之前`, `负责`, `跟进`, `定了`, `没问题`, `就这样`, `OK`/`ok`.
- Return `[mm:ss] text` (MM:SS for <1 h, HH:MM:SS otherwise).
- Group by topic if >5 matches; otherwise flat list.
- Always quote `text` verbatim — user needs verifiable audio pointers.

### 4.5 "Add hotwords and re-Run"

Tell user:
1. Expand "Advanced" in params panel.
2. Add each hotword on its own line in `hotwords` field:
   ```
   高通骁龙
   React.js
   张三
   XP-pen
   ```
3. Re-Run.

Hotwords work best for proper nouns/technical terms the base model would mis-recognize. Don't bias common words (`你好`, `谢谢`). Soft cap ~50 hotwords.

---

## 5. Known limitations (be honest with the user)

Zipformer-zh is specialized for clean Mandarin. Quality drops on:

- **Non-Mandarin languages** — produces Chinese-character mush. **Switch to `whisper-base`.**
- **Heavy code-switching (zh+en)** — English words transliterated into phonetic Chinese. **Switch to `whisper-base` with `language=auto`** for >10% English content.
- **Strong regional accents/dialects** — Cantonese, Min, Hakka, Wu, heavy Sichuan/northeastern. Light Putonghua accents OK; full dialect not. `mandarin-light-accent.wav` covers acceptable range.
- **Noisy/far-field audio** — background music, simultaneous speakers, >2 m from mic. Failures: hallucinated tokens, dropped segments, repetition. Suggest re-record or denoise first.
- **Singing/music with lyrics** — possible but unreliable.
- **Very long audio (>120 s)** — input caps at 120 s. Suggest client-side splitting; timestamps reset per chunk (user must re-offset).
- **Speaker identification/diarization** — **not supported.** No `speaker` field. (Whisper-base also can't.)
- **Word-level timestamps** — **not supported.** Segment-level only.
- **Per-segment confidence** — **not supported.** See §3.3.

"Wrong text" reports: check segment duration and whether audio is clean Mandarin. Short segments (<1 s) and non-Chinese content are usual suspects.

---

## 6. Zipformer-zh vs. whisper-base — when to redirect

This Pack and `whisper-base` overlap for Chinese audio:

| Dimension | `zipformer-zh` | `whisper-base` |
|-----------|-----------------|----------------|
| Languages | Mandarin Chinese only | ~99 (multilingual) |
| Translate-to-English | ❌ | ✅ via `task=translate` |
| Speed | ~3 s for 30 s audio | ~6 s for 30 s audio (HTP, beam=5) |
| Quality on clean Mandarin | Slightly better, plus hotwords | Good |
| Quality on code-switched zh+en | Poor (English butchered) | OK (multilingual) |
| Hotword bias | ✅ via `hotwords` param | ❌ |
| Per-segment confidence (`conf`) | ❌ | ✅ |
| Long-audio support | Same (≤120 s; VAD chunking) | Same |

Redirect logic (user on this Pack, needs different behavior):

- "Translate to English" / non-Chinese output → **redirect `whisper-base`** `task=translate`. Give switch instructions (§4.3).
- Audio has lots of English / code-switched → **redirect `whisper-base`**. Don't fix transliterated English — it's lossy.
- "Need confidence score per segment" → **redirect `whisper-base`** (only it has `conf`). Or accept §3.3.
- "Handle Cantonese / Japanese / English" → **redirect `whisper-base`**.
- Domain hotwords + clean Mandarin → **stay on zipformer-zh**, use `hotwords`.
- Speed-critical + clean Mandarin only → **stay on zipformer-zh**.

Don't volunteer this comparison unless user is choosing or has hit a limitation.

---

## 7. What you (the LLM) should NOT do

- **Do not re-run just to interpret an existing result.** If Run result is in context, interpret it. You MAY call `appbuilder_run` to verify I/O for WebUI, but re-running with changed params is the user's job.
- **Do NOT MODIFY** these files (developer-maintained). You MAY `read` `runner.py` READ-ONLY for understanding I/O. Run inference via HTTP API / `appbuilder_run` — never execute `runner.py` in generated app.
- ❌ **Do not invent fields** not in schema. Output has only `fullText` and `segments[].{start, end, text}`. **No** `conf`, `language`, `task`, `speaker`, `words`.
- ❌ **Do not "fix" recognized text silently.** If transcript says "锐艾克特" and you suspect "React", you may **suggest** correction but flag as guess, quote original `text` + `[start–end]`. Real fix → redirect `whisper-base` (§6).
- ❌ **Do not promise word-level timestamps, speaker labels, or per-segment confidence** — not in output.
- ❌ **Do not pretend zipformer-zh can translate.** Redirect to `whisper-base` with `task=translate`.

---

## 8. Quick reference — example output

30 s clean Mandarin clip, `vad=true`, `hotwords=""`:

```json
{
  "fullText": "今天的天气非常好。 我们去公园散步吧。 顺便买点水果回来。",
  "segments": [
    { "start": 0.00,  "end": 3.40,  "text": "今天的天气非常好。" },
    { "start": 3.40,  "end": 7.20,  "text": "我们去公园散步吧。" },
    { "start": 7.20,  "end": 11.60, "text": "顺便买点水果回来。" }
  ]
}
```

With hotwords (`骁龙`, `高通`), tech-talk clip:

```json
{
  "fullText": "高通骁龙处理器在端侧大模型上有优势。 这是因为 NPU 的能效比很高。",
  "segments": [
    { "start": 0.00, "end": 4.20, "text": "高通骁龙处理器在端侧大模型上有优势。" },
    { "start": 4.20, "end": 8.10, "text": "这是因为 NPU 的能效比很高。" }
  ]
}
```

No `language`, `task`, or `conf` field — by design (§3.1–§3.3).

If you see those fields in a Run result, it's NOT from this Pack — likely whisper-base output pasted in. Read Pack identity from Run metadata header, not from inferring based on output shape.
