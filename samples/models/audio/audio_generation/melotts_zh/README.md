# MeloTTS-ZH — Chinese Text-to-Speech on Snapdragon NPU

## Overview

**MeloTTS-ZH** is a high-quality Chinese text-to-speech (TTS) model that runs on
the Snapdragon NPU (HTP) via QAI AppBuilder. It converts Chinese text into a
natural-sounding WAV audio file using a 3-stage neural pipeline (encoder → flow →
HiFi-GAN decoder), with text preprocessing (G2P + BERT features) on the CPU.

- **Task**: Text-to-Speech (TTS)
- **Language**: Chinese (中文)
- **Output**: 44100 Hz mono WAV audio
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite; also runs on x86/AMD64 Windows
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [melotts_zh](https://aihub.qualcomm.com/compute/models/melotts_zh)

## Model Architecture

MeloTTS-ZH uses a 3-stage NPU pipeline, each stage running as a separate QNN
context binary. Text preprocessing runs on the CPU with MeloTTS.

| Stage | Model File | Device | Description |
| ----- | ---------- | ------ | ----------- |
| 0. Text preprocessing | (MeloTTS + HF BERT) | CPU | Text → phones / tones / language ids + BERT features |
| 1. Text Encoder | `encoder.bin` | NPU | Produces `m_p`, `logs_p`, `w_ceil`, `y_lengths`, `x_mask`, `g` |
| 2. Flow | `flow.bin` | NPU | Transforms encoder output into latent features `z` |
| 3. HiFi-GAN Decoder | `decoder.bin` | NPU | Converts latent features `z` into audio waveform (chunked) |

> `flow.bin` and `decoder.bin` use `uint16` quantized (native) I/O. `encoder.bin`
> uses `float32` I/O. See `python/NOTES.md` for the quantization parameters and
> the full pipeline diagram.

## Environment Setup

MeloTTS-ZH has a broad dependency tree with several platform-specific pitfalls,
so plain `pip install melotts` **does not work**. Two ways to set it up:

> ⚠️ **Dependencies are per-interpreter.** Each Python install (x86/AMD64 vs
> ARM64, 3.12 vs 3.13) has its **own** `site-packages`. If you set up under one
> interpreter and then run `melotts_zh.py` with a **different** one, you get
> `ModuleNotFoundError: No module named 'melo'`. Check which Python you're on:
> ```bat
> python -c "import sys, platform; print(sys.version, platform.machine())"
> ```
> On Windows-on-Snapdragon a native **ARM64** Python is recommended. On a
> standard x86/AMD64 PC any Python 3.10–3.13 works. Re-run the setup for
> whatever interpreter you intend to run the sample with.

### Option A — one-shot script (recommended)

```bat
cd models\audio\audio_generation\melotts_zh\python
setup_env.bat
```

`setup_env.bat` reproduces the full verified procedure below. It auto-detects
the Python interpreter and its architecture:

- **ARM64 (WoS)**: tries `Python313-arm64`, `Python312-arm64`, `Python311-arm64`
  (under `%LOCALAPPDATA%\Programs\Python\` and `C:\`) in that order.
- **x86/AMD64**: tries `Python313`, `Python312`, `Python311` (under
  `%LOCALAPPDATA%\Programs\Python\`, `C:\`, and `C:\Program Files\`) in that order.
- **Fallback**: whatever `python` resolves to on `PATH`.

It is safe to re-run. To force a specific interpreter, set `PYTHON_EXE` before
calling the script:
```bat
set PYTHON_EXE=C:\Python312\python.exe
setup_env.bat
```

The script caches the downloaded melotts sdist tarball under
`../models/pip_cache/` (a sibling of `python/`) so re-runs skip the download.

### Option B — manual steps

If you prefer to run the steps yourself (or need to adapt them), this is exactly
what the script does. `python/NOTES.md` has the deeper background on each item.

**1. Core runtime (WITH deps) + torch:**
```bat
pip install qai_appbuilder numpy soundfile requests
pip install torch --index-url https://download.pytorch.org/whl/cpu
```
> The PyTorch CPU index is used for **both** ARM64 and x86/AMD64:
> - ARM64: the default PyPI index has no `win_arm64` torch wheel.
> - x86/AMD64: the CPU index avoids accidentally pulling a large CUDA-enabled
>   wheel from the default index.

**2. Install `melotts` from a PATCHED sdist** (NOTES.md Step 4). The PyPI sdist
ships a `setup.py` that reads a `requirements.txt` it does not include, so a
normal install fails with `FileNotFoundError: ... requirements.txt`. Work around
it by injecting an empty file:
```bat
curl -k -L -o melotts-0.1.1.tar.gz https://files.pythonhosted.org/packages/source/m/melotts/melotts-0.1.1.tar.gz
tar -xzf melotts-0.1.1.tar.gz
type nul > melotts-0.1.1\requirements.txt
pip install --no-deps --no-build-isolation melotts-0.1.1
```

**3. Install the rest of the deps with `--no-deps`:**
```bat
pip install --no-deps -r requirements.txt
```
> `--no-deps` is required: MeloTTS' dependency chain would otherwise pull
> build-only / heavyweight packages (e.g. `numba`, `llvmlite`) that have no
> ARM64 wheel and are also unnecessary on x86/AMD64. The unavailable native
> modules (`torchaudio`, `numba`, `soxr`, `boto3`, `melo.text.korean`) are
> stubbed **in-process** by `melotts_zh.py` §1, so they are intentionally NOT
> installed.
>
> `requirements.txt` pins the tightly-coupled HuggingFace triple
> (`transformers==4.56.2` / `tokenizers==0.22.2` / `huggingface_hub==0.34.4`) and
> includes several transitive deps that `--no-deps` would otherwise miss
> (`proces`, `nltk`, `typeguard`, `inflect`, ...).

**4. Patch `melo/text/japanese.py`** (NOTES.md Step 6). MeloTTS' `japanese.py`
hard-fails to import when MeCab / the Japanese BERT tokenizer are absent (neither
is available/needed on WoS or standard x86/AMD64). Three edits make the import safe:
```bat
python patch_melo_japanese.py
```
This helper is idempotent and prints `[SKIP]` for edits already applied.

### Troubleshooting

**`ModuleNotFoundError: No module named 'melo'`** — the most common cause is
running the sample with a **different interpreter** than the one you set up
(e.g. you set up under x86/AMD64 Python 3.12 but `python` now resolves to ARM64
Python 3.13). Dependencies are per-interpreter (see the callout above). Confirm
which Python you're on, then re-run `setup_env.bat` for it (or do step 2
manually). This error also appears if `pip install melotts` was attempted
directly and silently failed — always use the patched-sdist route in step 2.

**`ModuleNotFoundError` for `proces` / `nltk` / `typeguard` / `inflect` / …** — a
transitive dep is missing (these are pulled in by MeloTTS' chain but skipped by
`--no-deps`). Re-run step 3 (`pip install --no-deps -r requirements.txt`), which
lists them explicitly.

**`ImportError: ... is required for a normal functioning of this module`
(huggingface_hub / tokenizers)** — the HuggingFace version triple is wrong. A
fresh Python install often ships `transformers` 5.x / `huggingface_hub` 1.x
pre-installed; step 2/3 downgrade them to the required
`transformers==4.56.2` / `tokenizers==0.22.2` / `huggingface_hub==0.34.4`. Re-run
step 3 to force the pins.

**`error: [Errno 28] No space left on device` during install** — several deps
(`jieba`, `gruut-lang-*`, `distance`, …) ship only as sdists and are built in a
temp dir, which needs free disk. Free up space (e.g. `pip cache purge` reclaims
the download cache) and re-run. The full install footprint is a few GB including
torch and the first-run HF model.

### First-run downloads

On the first run, `melo.text.chinese_bert` downloads the
`hfl/chinese-roberta-wwm-ext-large` HF model (~1.3 GB) from `huggingface.co`, and
the model `.bin` files (~316 MB) auto-download into `../models/` (see
[Model Download](#model-download)). Both need network access.

## Quick Start

First set up the environment once (see [Environment Setup](#environment-setup)):
```bat
cd models\audio\audio_generation\melotts_zh\python
setup_env.bat
```

Then run — via the sample launcher:
```bash
cd qai-appbuilder\samples
python run_inference.py --model melotts_zh --args "--text '中文是中国的语言文字，包括汉语和汉字。'"
```

Or run the script directly:
```bash
python models\audio\audio_generation\melotts_zh\python\melotts_zh.py --text "中文是中国的语言文字，包括汉语和汉字。"
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--text` | `"中文是中国的语言文字，包括汉语和汉字。"` | Input Chinese text to synthesize |
| `--out` | `python/output.wav` | Output WAV file path |
| `--noise_scale` | `0.667` | Noise scale (controls voice variation) |
| `--noise_scale_w` | `0.8` | Duration noise scale |
| `--length_scale` | `1.0` | Speech rate (>1 is slower, <1 is faster) |
| `--sdp_ratio` | `0.2` | Stochastic duration predictor ratio |

## Model Download

Models are **downloaded automatically on first run** into the `models/` folder
(a sibling of `python/`). The script:

1. Detects your device via `install.detect_device_model()` (Snapdragon X Elite /
   X2 Elite) and picks the matching AI Hub ZIP.
2. Downloads + extracts it via `install.download_url()` (the shared helper in
   `samples/shared/python/install.py`).
3. Fetches the MeloTTS hparams config (`melo_config_zh.json`) from HuggingFace
   (`myshell-ai/MeloTTS-Chinese/resolve/main/config.json`; the upstream
   `melo.download_utils` URL returns 403).

The extracted ZIP contains `encoder.bin` / `flow.bin` / `decoder.bin` (plus
`metadata.json` / `config.json`). To relocate the working directory, set the
`MELOTTS_WORK_DIR` environment variable.

Model ZIP URLs by chipset:

| Chipset | Release | URL |
| ------- | ------- | --- |
| Snapdragon X Elite | v0.55.0 | `qaihub-public-assets.s3.us-west-2.amazonaws.com/.../snapdragon_x_elite.zip` |
| Snapdragon X2 Elite | v0.56.0 | `qaihub-public-assets.s3.us-west-2.amazonaws.com/.../snapdragon_x2_elite.zip` |

> ⚠️ The `.bin` files are chipset-specific — do **not** mix X Elite / X2 Elite
> binaries. The script picks the right ZIP for the detected chipset automatically.
> If the chipset is not recognized, it falls back to the X Elite ZIP with a
> `[WARN]` message.

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | UTF-8 string | Chinese text |
| **Output** | WAV file | 44100 Hz, mono |

## Pipeline Details

```
Text (中文)
    ↓ MeloTTS get_text_for_tts_infer()   [CPU]
phones / tones / lang_ids [int32]  +  bert_feat[1024,512], ja_bert_feat[768,512]
    ↓ encoder.bin   [float32 I/O, NPU]
m_p[1,192,512], logs_p[1,192,512], w_ceil[1,1,512], y_lengths, x_mask, g[1,256,1]
    ↓ generate_path()   [CPU]  → attn_squeezed[1,1536,512]
    ↓ flow.bin   [uint16/native I/O, NPU]
z [1, 192, 1536]
    ↓ decoder.bin × 24 chunks   [uint16/native I/O, NPU]
Audio waveform [float32]
    ↓ trim to y_lengths × 512 samples, normalize peak to 0.9 if > 1.0
    ↓ soundfile.write
output.wav (44100 Hz, mono)
```

### Key constants

| Constant | Value | Description |
| -------- | ----- | ----------- |
| `MAX_SEQ_LEN` | 512 | Maximum phoneme sequence length (encoder input) |
| `UPSAMPLED_MAX_SEQ_LEN` | 1536 | Maximum upsampled sequence length (flow input) |
| `MAX_DEC_SEQ_LEN` | 64 | Decoder chunk size along the time axis |
| `UPSAMPLE_FACTOR` | 512 | Samples per latent frame (used to trim final audio) |
| `SAMPLE_RATE` | 44100 | Output audio sample rate (Hz) |

### Quantization

`flow.bin` and `decoder.bin` use `uint16` quantization. QNN reports the dtype as
`ufp16`, but the bits are actually `uint16`. The script converts between
`float32` and `uint16` using the parameters from `metadata.json`:

```
quantize:   q = clip(round(x / scale) + zero_point, 0, 65535).astype(uint16)
            then view as float16 to pass to QNN
dequantize: receive float16 from QNN, view as uint16,
            then x = (q.astype(float32) - zero_point) * scale
```

Quantization parameters used by the script:

| Model | Tensor | Scale | Zero-point |
| ----- | ------ | ----- | ---------- |
| flow (in) | `attn_squeezed` | 3.052e-05 | 32768 |
| flow (in) | `logs_p` | 3.420e-05 | 22619 |
| flow (in) | `noise_scale` | 1.018e-05 | 0 |
| flow (in) | `m_p` | 7.963e-05 | 34950 |
| flow (in) | `y_mask` | 1.526e-05 | 0 |
| flow (in) | `g` | 2.008e-05 | 36547 |
| flow (out) | `z` | 2.913e-04 | 32329 |
| decoder (in) | `g` | 2.008e-05 | 36547 |
| decoder (in) | `z` | 2.923e-04 | 32350 |
| decoder (out) | `audio` | 1.307e-05 | 35961 |

### NPU performance profile

The HTP performance profile is raised to `BURST` **once** before the encoder
stage and held across all three NPU stages (encoder → flow → decoder). It is
released only after the last decoder chunk completes. This avoids the HTP clock
ramping up and down between stages, which would add latency.

## Platform Notes

| Platform | Python | Notes |
| -------- | ------ | ----- |
| Windows on Snapdragon (WoS) | ARM64 3.11–3.13 (recommended) | Native ARM64 Python gives best performance; `setup_env.bat` auto-detects it |
| Windows x86/AMD64 | x86/AMD64 3.10–3.13 | Fully supported; `setup_env.bat` auto-detects and uses the CPU PyTorch index |

> On x86/AMD64 the NPU stages still run on the Snapdragon HTP if the hardware is
> present. On a standard x86 PC without Snapdragon NPU, the QNN HTP runtime will
> not be available — the sample is primarily intended for Snapdragon-equipped
> Windows devices.

## Notes

- The full inference (encoder + flow + 24 decoder chunks on HTP) takes ~3 s of
  NPU compute after models are loaded.
- The final audio is trimmed to `y_lengths × 512` samples (the exact speech
  duration predicted by the encoder) and peak-normalized to 0.9 if the absolute
  maximum exceeds 1.0. Any NaN values are replaced with 0 before writing.
- A harmless `<E> Error 0x200: failed to close queue ...` line may print on exit.
  It is a QNN HTP context-teardown warning emitted **after** the WAV is written
  and does not affect the output.
- Files in this `python/` folder:
  - `melotts_zh.py` — the inference script.
  - `setup_env.bat` — one-shot environment setup (deps + melotts sdist + patch);
    supports both ARM64 and x86/AMD64 Python with automatic architecture detection.
    Caches the melotts sdist tarball under `../models/pip_cache/`.
  - `requirements.txt` — the `--no-deps` install list (pins the HF triple);
    compatible with both ARM64 and x86/AMD64.
  - `patch_melo_japanese.py` — applies the three `japanese.py` edits (idempotent).
  - `NOTES.md` — **required reading** for the deeper background: every dependency,
    patch, and in-process stub (`torchaudio` / `numba` / `soxr` / `boto3` /
    `melo.text.korean` mocks, the `melo.download_utils` monkey-patch, the three
    `melo/text/japanese.py` edits) and the verified output ranges.
- Output is saved to `python/output.wav` by default.
