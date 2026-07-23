# zipformer-zh — App Builder Pack

Streaming Zipformer RNN-T for Mandarin Chinese ASR on Snapdragon QNN HTP,
INT8 quantized. Three-stage encoder/decoder/joiner pipeline; outputs
time-aligned Chinese segments. Lighter and ~2× faster than `whisper-base`
on clean Mandarin, but Chinese-only with no translation.

This Pack is fully isolated from `features/model-builder/` (no
cross-imports — that boundary is enforced by CI).

## Implementation status

`runner.py` is a **production-ready** end-to-end streaming runner ported
from the QAI AppBuilder reference sample
(`samples/python/zipformer/zipformer.py`). It implements the full
chunked encoder + cache-state pipeline that the QNN context binaries
were exported for:

- Loads `encoder.bin` / `decoder.bin` / `joiner.bin` via
  `shared/qnn_helper.QnnContext` with `DataType.NATIVE` so the encoder's
  mixed `int32` / `float32` I/O flows through correctly (the encoder's
  per-layer `cached_len` slots are int32; coercing them to float
  silently breaks decoding with no error).
- Computes 80-channel kaldi-style log-mel fbank via `kaldi_native_fbank`
  (matches sherpa-onnx training recipe: `dither=0`, `snip_edges=False`,
  `high_freq=Nyquist−400 Hz`).
- Streams the audio through the encoder in `OFFSET=64`-frame chunks
  (`SEGMENT=71` with the 7-frame look-ahead), threading the 35-tensor
  cache state across iterations exactly as the reference sample does.
- Performs frame-by-frame greedy RNN-T decoding over the joiner's logits
  (`VOCAB_SIZE=6254`), refreshing the decoder embedding only when a
  non-blank token is emitted (≈80 % of joiner calls reuse the cached
  decoder state).
- Detokenizes via `assets/tokens.txt` (sherpa-onnx `<token> <id>`
  format, ~6257 entries). Strips SentencePiece word-start (`▁`) and
  WordPiece (`##`) prefixes; suppresses `<blk>` / `<sos/eos>` / `<unk>`.
- Emits a single segment covering the whole clip (MVP); v1.x will plug
  in silence-driven segment splitting.

The HTP runtime is automatically switched into the `BURST` perf profile
around the inference loop when the qai_appbuilder build supports it.

## Dependency layout

| Path | Source / Purpose |
|---|---|
| `models/zipformer-zh/encoder.bin` (~144 MB) | Real model from `qaihub-public-assets`. Auto-downloaded on first run if missing; the runner falls back to `weights/encoder.bin` only if a user has manually staged it there. |
| `models/zipformer-zh/decoder.bin` (~7 MB) | Same |
| `models/zipformer-zh/joiner.bin` (~6 MB) | Same |
| `models/zipformer-zh/metadata.json` (~1 KB, optional) | Bundled in the qai-hub zip; copied alongside the bins for traceability. |
| `assets/tokens.txt` (~62 KB, ~6257 lines) | Bundled — sherpa-onnx Mandarin Zipformer vocab |
| `examples/mandarin-{dialog,reading,light-accent}.wav` | Bundled — three short Mandarin clips for first-run smoke testing |

### Auto-download (first-run convenience)

When `models/zipformer-zh/` is missing one or more of the 3 `.bin`
files, the runner mirrors the QAI AppBuilder reference sample
(`samples/python/zipformer/zipformer.py · ensure_model_files`):

1. Detects the Snapdragon SoC family via WMI (`Win32_Processor.Name`).
   - `family 8 model 1` / `x elite` → `snapdragon_x_elite`
   - `family 8 model 2` / `x2` → `snapdragon_x2_elite`
   - Anything else → defaults to `snapdragon_x_elite` (the v73 HTP
     binary works on both, just slightly slower on X2).
2. Downloads
   `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/.../zipformer-qnn_context_binary-float-qualcomm_snapdragon_x{,2}_elite.zip`
   (~125 MB) into `models/zipformer-zh/`. Progress is streamed to the
   front end as `{"type":"progress","phase":"download","pct":N}` events.
3. Extracts the zip, copies `encoder.bin` / `decoder.bin` / `joiner.bin`
   (and optionally `metadata.json`) into `models/zipformer-zh/`,
   removes the temp dir and the archive.
4. Subsequent runs short-circuit on the existence check and never hit
   the network — the download is purely a first-run convenience.

If the download or extraction fails, the runner emits a
`WEIGHTS_NOT_INSTALLED` error event with a manual-placement hint and
exits 1. To bypass auto-download (e.g. air-gapped install), drop the
3 `.bin` files into `models/zipformer-zh/` before the first run.

If you regenerate the model (different hardware target / different
QAIRT version), the manifest `assets.weightsUrl` points at the v0.54.0
public-assets zip the existing files were extracted from.

## Manual smoke test (no UI required)

From the repo root, on Windows PowerShell:

```pwsh
$env:PYTHONPATH = "$PWD\features\app-builder\shared"
'{"runId":"r-test","modelId":"zipformer-zh",
 "inputs":{"audio":"features/app-builder/models/zipformer-zh/examples/mandarin-reading.wav"},
 "params":{"language":"zh","vad":true,"hotwords":""},
 "options":{},
 "packDir":"features/app-builder/models/zipformer-zh",
  "repoRoot":"."}' | & "$env:LOCALAPPDATA\QAIModelBuilder\envs\.venv_arm64_313\Scripts\python.exe" features\app-builder\models\zipformer-zh\runner.py
```

Expected (real Mandarin clip + real weights): one `status` event,
`metrics`, `result`, `done` line-JSON events on stdout; the recognized
text appears in `result.output.fullText`.

When the inputs are missing, the runner emits a single `error` event
with one of `INVALID_INPUT` / `WEIGHTS_NOT_INSTALLED` /
`ASSETS_NOT_INSTALLED` / `QAI_APPBUILDER_UNAVAILABLE` /
`TOKENIZER_LOAD_ERROR` / `AUDIO_DECODE_ERROR` / `AUDIO_TOO_LONG` /
`OUT_OF_MEMORY` / `INFER_ERROR` and exits 1.

## Algorithm simplifications (MVP, called out for v1.x)

- **Single segment per clip** — `segments=[{start:0, end:duration,
  text:...}]`. v1.x will implement the silence-driven segment splitter
  described in `SKILL.md` §3.4.
- **No VAD** — `params.vad` is parsed but unused. v1.x will plug in a
  Silero / WebRTC VAD pre-pass.
- **No hotword biasing** — `params.hotwords` is parsed but ignored. The
  joiner runs pure greedy. v1.x will add a context-graph score-boost.
- **No streaming output** — the encoder streams internally (matches the
  cache-state pipeline) but the runner only emits `result` once at the
  end. v1.x may emit per-chunk partial results when the front end can
  render them.
- **No per-segment confidence** — RNN-T greedy decoding does not
  produce a calibrated `conf` (see `SKILL.md` §3.3); the schema
  deliberately excludes it.

## SKILL.md is enabled

`manifest.skill.enabled = true`. When the user selects this Pack and
sends a message in App Builder mode, `skill_resolver` injects two
SKILL files into the system prompt: `features/app-builder/SKILL.md`
(top-level) and `features/app-builder/models/zipformer-zh/SKILL.md`
(this Pack's per-model contract). Keep both in sync if the manifest
output schema or parameter set changes.
