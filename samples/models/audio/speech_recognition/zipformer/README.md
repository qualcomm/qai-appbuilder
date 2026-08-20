# Zipformer — Streaming Chinese/English ASR on Snapdragon NPU

## Overview

**Zipformer** is a streaming Automatic Speech Recognition (ASR) model that runs on
the Snapdragon NPU (HTP) via QAI AppBuilder. It transcribes Mandarin Chinese and
English (mixed) speech using a 3-stage RNN-T pipeline (encoder → joiner → decoder)
with chunked streaming inference.

- **Task**: Automatic Speech Recognition (ASR)
- **Language**: Mandarin Chinese + English (mixed)
- **Input**: WAV audio file (16 kHz mono preferred; auto-resampled if needed)
- **Output**: Transcribed text string
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU) with native data types (float32 / int32)
- **AI Hub Model**: [Zipformer](https://huggingface.co/qualcomm/Zipformer)

## Model Architecture

Zipformer uses a streaming RNN-T architecture split into three QNN context binaries.
All three run on the NPU via `qai_appbuilder.QNNContext`.

| Stage | Model File | I/O | Description |
| ----- | ---------- | --- | ----------- |
| 1. Encoder | `encoder.bin` | float32 / int32 (native) | Chunked streaming Zipformer encoder; 35 cache tensors I/O + `encoder_out [1,16,512]` |
| 2. Decoder | `decoder.bin` | int32 → float32 (native) | RNN-T prediction network; 2-token context → `decoder_out [1,512]` |
| 3. Joiner  | `joiner.bin`  | float32 (native) | `encoder_out [1,512]` × `decoder_out [1,512]` → logit `[1,6254]` |

> All three `.bin` files use `input_data_type="native"` + `output_data_type="native"`
> because the encoder cache mixes int32 (`cached_len_*`) and float32 tensors.

## Requirements

```
pip install qai_appbuilder numpy soundfile scipy kaldi_native_fbank
```

> `kaldi_native_fbank` is strongly recommended for accurate feature extraction
> (matches the sherpa-onnx training recipe). If unavailable, the script falls back
> to a pure-numpy STFT implementation.
>
> Install the ARM64 wheel matching your Python version:
> ```
> pip install kaldi_native_fbank
> ```
>
> **`ffmpeg`** (optional, on PATH) enables the silence-detection VAD chunking that
> is **on by default** — it skips silent regions and splits long audio at natural
> pauses before decoding. If `ffmpeg` is not found (or fails), the script silently
> falls back to decoding the whole waveform in one pass. Pass `--no-use_vad` to
> disable VAD explicitly.

## Quick Start

```bash
cd qai-appbuilder\samples

# Run with the auto-downloaded test audio (Chinese speech)
python run_inference.py --model zipformer

# Run with a custom audio file
python run_inference.py --model zipformer --args "--audio path\to\audio_16khz.wav"
```

Or run the script directly:
```bash
python models\audio\speech_recognition\zipformer\python\zipformer.py
python models\audio\speech_recognition\zipformer\python\zipformer.py --audio path\to\audio.wav
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--audio` | `models/test_zh.wav` (auto-downloaded) | Path to input WAV (16 kHz mono preferred) |
| `--model_dir` | Auto-downloaded model directory | Directory holding `encoder.bin` / `decoder.bin` / `joiner.bin` |
| `--tokens` | `tokens.txt` inside model directory | Path to sherpa-onnx `tokens.txt` vocabulary file |
| `--use_vad` / `--no-use_vad` | on | Enable/disable silence-detection VAD chunking (requires `ffmpeg` on PATH; falls back to a single-segment decode if unavailable) |
| `--vad_noise_db` | `-35.0` | `[--use_vad]` `silencedetect` noise floor (dB) |
| `--vad_min_silence` | `0.6` | `[--use_vad]` Minimum silence duration (s) to split at |
| `--vad_overlap` | `0.3` | `[--use_vad]` Segment overlap (s) |
| `--vad_min_speech` | `1.0` | `[--use_vad]` Discard speech segments shorter than this (s) |
| `--vad_max_segment` | `25.0` | `[--use_vad]` Max segment length (s); longer segments are split with overlap |

## Model Download

### Working Directory (`WORK_DIR`)

All downloaded files are stored in the **working directory** (`WORK_DIR`).
By default this is the `models/` folder that sits next to the `python/` folder:

```
zipformer/
├── python/
│   └── zipformer.py        ← inference script
└── models/                 ← WORK_DIR (auto-created on first run)
    ├── tokens.txt           ← vocabulary (auto-downloaded from GitHub)
    ├── test_en.wav          ← English test audio (auto-downloaded from AI Hub S3)
    ├── test_zh.wav          ← Chinese test audio (auto-downloaded from HuggingFace)
    └── zipformer-qnn_context_binary-float-qualcomm_snapdragon_x_elite/
        ├── encoder.bin
        ├── decoder.bin
        ├── joiner.bin
        └── metadata.json
```

To use a different location, set the environment variable before running:
```bat
set ZIPFORMER_WORK_DIR=C:\MyModels\zipformer
python zipformer.py
```

### What Gets Downloaded

On first run the script automatically downloads:

1. **Model ZIP** — Detects your device via `install.detect_device_model()`
   (Snapdragon X Elite / X2 Elite), picks the matching AI Hub ZIP, downloads and
   extracts it via `install.download_url()` (the shared helper in
   `samples/shared/python/install.py`). The ZIP contains `encoder.bin` /
   `decoder.bin` / `joiner.bin` / `metadata.json`.

2. **`tokens.txt`** (vocabulary, 6254 BPE tokens) — **NOT included in the ZIP**,
   downloaded separately from the official `qualcomm/ai-hub-models` GitHub repo:
   `src/qai_hub_models/models/zipformer/tokens.txt` → saved to `WORK_DIR/tokens.txt`.
   Without this file the output will be raw token ID numbers instead of text.

3. **Test audio** — see [Test Audio Download](#test-audio-download) below.

> ⚠️ The `.bin` files are chipset-specific — do **not** mix X Elite / X2 Elite
> binaries. The script picks the right ZIP for the detected chipset automatically.

### Test Audio Download

The script also auto-downloads a test audio file on first run. Two sources are
tried in order (first successful download wins):

| Priority | File | Source | URL |
| -------- | ---- | ------ | --- |
| 1 (primary, **default**) | `test_zh.wav` | **HuggingFace / sherpa-onnx** — Chinese speech, ~5.6 s, 16 kHz mono | `https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23/resolve/main/test_wavs/0.wav` |
| 2 (fallback) | `test_en.wav` | **AI Hub public S3** — English (Common Voice), ~15 s, 241 KB. This is the same audio used by the official `qai_hub_models` demo (`CachedWebModelAsset.from_asset_store("hf_whisper_asr_shared", "1", "audio/common_voice_en_19653650.wav")`) | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/hf_whisper_asr_shared/v1/audio/common_voice_en_19653650.wav` |

Both files are 16 kHz mono WAV. `test_zh.wav` is the default — if HuggingFace is
unreachable, the script falls back to `test_en.wav` from the AI Hub public S3
bucket (no authentication required).

### Manual Download URLs (v0.55.0)

| Chipset | URL |
| ------- | --- |
| Snapdragon X Elite | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/zipformer/releases/v0.55.0/zipformer-qnn_context_binary-float-qualcomm_snapdragon_x_elite.zip` |
| Snapdragon X2 Elite | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/zipformer/releases/v0.55.0/zipformer-qnn_context_binary-float-qualcomm_snapdragon_x2_elite.zip` |

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | WAV file | 16 kHz mono preferred; auto-resampled via `scipy.signal` if needed |
| **Output** | String | Transcribed Mandarin Chinese / English text |

## Pipeline Details

```
Audio file (WAV, 16 kHz mono)
    ↓ soundfile.read + optional scipy resample
    ↓ VAD segmentation (ffmpeg silencedetect; on by default, --no-use_vad to skip)
      → list of speech segments, silent regions skipped, long segments split
    ↓ per segment: kaldi_native_fbank log-mel (80 bins, dither=0, snip_edges=False, high_freq=-400)
Features [T, 80] float32  (per segment; each decoded on its own encoder state)
    ↓ for each 71-frame chunk (stride=64):
        Encoder (NPU, native I/O)
            inputs : chunk [1,71,80] + 35 cache tensors (int32/float32)
            outputs: 35 new cache tensors + encoder_out [1,16,512]
        for t in 0..15:
            Joiner (NPU, native I/O)
                inputs : encoder_out[t] [1,512] + decoder_out [1,512]
                outputs: logit [1,6254]
            argmax → if not blank(0): append token
            Decoder (NPU, native I/O)
                inputs : last 2 token ids [1,2] int32
                outputs: decoder_out [1,512]
Token IDs
    ↓ tokens.txt (sherpa-onnx format)
Transcribed text (string)
```

## Performance

On Snapdragon X2 Elite (verified):
- RTF ≈ 0.02 (50× faster than real-time)
- Example output for `test_zh.wav`:
  `「对我做了介绍啊那么我想说的是呢大家如果对我的研究感兴趣」`

## Example Output

```
[INFO] Checking / downloading Zipformer models ...
[INFO] Detected device model: snapdragon_x2_elite
[INFO] Model dir : ...\models\zipformer-qnn_context_binary-float-qualcomm_snapdragon_x2_elite
[INFO] Tokens    : ...\models\zipformer-qnn_context_binary-float-qualcomm_snapdragon_x2_elite\tokens.txt

============================================================
  Zipformer streaming ASR — qai_appbuilder / QNNContext (NPU)
============================================================

[1] Loading audio: ...\models\test_zh.wav
    Duration : 5.63 s  |  Samples: 90080  |  SR: 16000

[2] VAD segmentation (ffmpeg silencedetect) ...
    Segments : 1  (0.043s)

[3] Extracting 80-dim log-mel features (per segment) ...
    Total frames: 351  (0.012s)

[4] Loading vocabulary ...
    Vocab size: 6254

[5] Loading QNN context binaries (encoder/decoder/joiner.bin) on HTP ...
    Load time: 1.23s

[6] Running streaming greedy search ...

============================================================
  RECOGNITION RESULT
============================================================
  Text     : 对我做了介绍啊那么我想说的是呢大家如果对我的研究感兴趣
  Tokens   : 31
  Segments : 1  (VAD on)
  Audio    : 5.63s
  Infer    : 0.11s
  RTF      : 0.0195  (< 1.0 = faster than real-time)
============================================================
```

## Notes

- **Cache input order** (Issue Z-1): The encoder inputs are ordered **by layer**
  (not by type): `x`, then for each of the 5 layers: `len/avg/key/val/val2/conv1/conv2`.
  Wrong order causes silent hang or garbled output.
- **encoder_out is the last output** (Issue Z-2): `encoder_out [1,16,512]` is at
  index 35 (the last output), not the first. The first 35 outputs are the new cache.
- **Native dtype** (Issue Z-3): `cached_len_*` are int32; all other cache tensors
  and `x` are float32. All three models use `input_data_type="native"` /
  `output_data_type="native"` — do not uniformly cast to float.
- **Use real speech** (Issue Z-4): Synthetic audio (sine wave / silence) produces
  empty output. The auto-downloaded `test_zh.wav` is a real Chinese speech sample.
- **VAD chunking** is **on by default** and uses `ffmpeg`'s `silencedetect` filter
  to skip silent regions and split long audio at natural pauses; each speech
  segment is decoded on its own streaming encoder state so context doesn't leak
  across pauses. It requires `ffmpeg` on PATH — if `ffmpeg` is missing or fails,
  the script silently falls back to decoding the whole waveform in one pass.
  Disable it with `--no-use_vad` and tune it with the `--vad_*` arguments.
- A harmless `<E> Error 0x200: failed to close queue ...` may print on exit.
  It is a QNN HTP context-teardown warning and does not affect the output.
- Files in this directory:
  - `python/zipformer.py` — the inference script (auto-downloads models on first run).
  - `NOTES.md` — detailed model notes: I/O shapes, download URLs, pipeline geometry,
    known issues (Z-1 through Z-4), and the reference implementation.

## See Also

- [NOTES.md](NOTES.md) — full technical reference for this model
- [whisper_base_en](../whisper_base_en/) — English-only ASR (Whisper Base)
- [whisper_tiny_en](../whisper_tiny_en/) — English-only ASR (Whisper Tiny, faster)
- [MeloTTS-ZH](../../audio_generation/melotts_zh/) — Chinese TTS (text → speech)
