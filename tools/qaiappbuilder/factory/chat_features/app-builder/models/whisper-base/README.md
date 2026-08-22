# whisper-base — App Builder Pack (English)

OpenAI Whisper base.en (~74 M params) for English speech-to-text on
Snapdragon QNN HTP. Two-stage encoder/decoder pipeline; outputs English
text with chunk-level timestamps. Adapted from the QAI AppBuilder
reference sample
[`samples/python/whisper_base_en/whisper_base_en.py`](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/whisper_base_en).
This Pack is fully isolated from `features/model-builder/`
(no cross-imports).

## Pipeline

`runner.py` mirrors the reference sample and wraps it in the App Builder
runner protocol (plan v3.1 · C★.6 / I.4 / R.3):

  1. `read_audio` (`shared/audio_io.py`) → resample to 16 kHz mono.
  2. Split into ≤30 s sequential windows (no VAD in MVP).
  3. Per window: `torch.stft` + 80-channel mel filterbank
     (`assets/mel_filters.npz`, key `mel_80`) → `(1, 80, 3000)` float16.
  4. `encoder.bin` QNN inference → `k_cache_cross (6,8,64,1500)`,
     `v_cache_cross (6,8,1500,64)`.
  5. Greedy decoder loop (≤224 steps) with the standard Whisper
     timestamp-rule logits filter (suppress non-speech, suppress blank,
     pair timestamps, max-initial-timestamp clamp). Per-step inputs match
     the reference sample exactly:
     `(x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self)`.
  6. `tokenizer.decode(...)` (`whisper.decoding.get_tokenizer`,
     `multilingual=False, language='en'`) → segment text.
  7. Concatenate chunks → `fullText` + `segments[]`.

## Bundled assets

After integration this Pack ships with:

  * `<repo>/models/whisper-base/encoder.bin` (≈90 MB)
  * `<repo>/models/whisper-base/decoder.bin` (≈145 MB)
  * `assets/mel_filters.npz` (real torchaudio-derived filterbank)
  * `examples/jfk.wav` and `examples/jfk.npz` (≈11 s, public-domain)

`assets/tokenizer.json` is **not** consumed by this runner — the
English tokenizer is constructed at runtime via `openai-whisper`.

## Output schema

```json
{
  "language": "en",
  "task": "transcribe",
  "fullText": "...",
  "segments": [{"start": 0.0, "end": 30.0, "text": "...", "conf": 0.92}]
}
```

`task='translate'` is rejected with `INVALID_INPUT` because base.en is
English-only. Use a multilingual whisper variant for translation.

## Failure modes (structured `error` events)

| `code` | Trigger |
|---|---|
| `INVALID_INPUT` | missing `inputs.audio`, file not found, `task='translate'` |
| `WEIGHTS_NOT_INSTALLED` | `encoder.bin` and/or `decoder.bin` missing from both `models/whisper-base/` and `weights/` |
| `ASSETS_NOT_INSTALLED` | `mel_filters.npz` missing |
| `TOKENIZER_LOAD_ERROR` | `openai-whisper` import failed or `get_tokenizer` raised |
| `QAI_APPBUILDER_UNAVAILABLE` | `qai_appbuilder` import failed, or `QAIRT_LIB_DIR` / `config/qairt_env.json` is not configured |
| `AUDIO_DECODE_ERROR` | `read_audio` raised (corrupted file, missing ffmpeg for mp3/webm) |
| `AUDIO_TOO_LONG` | duration > 120 s (manifest `inputSchema.constraints.maxSec`) |
| `OUT_OF_MEMORY` | encoder/decoder OOM (heuristic) |
| `INFER_ERROR` | catch-all for unexpected QNN runtime / shape errors |

## Algorithm simplifications (called out for downstream device tests)

* **Long-audio chunking** is fixed 30 s sequential windows. No VAD —
  `params.vad` is accepted but not implemented.
* **Segment timestamps** are chunk boundaries (≤30 s granularity,
  ±0.5 s in practice). No `<|t...|>` timestamp-token decoding path.
* **Greedy** decoding only (no beam search, no temperature stepping,
  no length penalty, no repetition penalty).
* **`conf`** is `exp(mean(log p_chosen))` along the decoded path —
  a rough proxy, not a calibrated probability.

## SKILL.md is enabled

`manifest.skill.enabled = true`. When the user selects this Pack and
sends a message in App Builder mode, `skill_resolver` injects two
SKILL files into the system prompt: `features/app-builder/SKILL.md`
(top-level) and this Pack's `SKILL.md`.
