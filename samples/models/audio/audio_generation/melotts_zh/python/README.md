# MeloTTS-ZH — `python/` folder

This folder contains the Python inference script and environment-setup helpers
for the **MeloTTS-ZH** Chinese TTS sample.

## Files

| File | Description |
|------|-------------|
| [`melotts_zh.py`](#melotts_zhpy) | Main inference script — runs the full TTS pipeline on the Snapdragon NPU |
| [`setup_env.bat`](#setup_envbat) | One-shot environment setup (ARM64 and x86/AMD64) |
| [`requirements.txt`](#requirementstxt) | `--no-deps` dependency list with pinned HuggingFace versions |
| [`patch_melo_japanese.py`](#patch_melo_japanesepy) | Patches `melo/text/japanese.py` to be import-safe without MeCab |

---

## `melotts_zh.py`

The main inference script. Runs the complete MeloTTS-ZH pipeline:

```
Text (Chinese)
    ↓  MeloTTS get_text_for_tts_infer()  [CPU]
       phones / tones / lang_ids [int32]
       bert_feat [1024, 512]  +  ja_bert_feat [768, 512]
    ↓  encoder.bin  [float32 I/O, NPU]
       m_p [1,192,512], logs_p [1,192,512], w_ceil [1,1,512]
       y_lengths, x_mask [1,1,512], g [1,256,1]
    ↓  generate_path()  [CPU]  →  attn_squeezed [1,1536,512]
    ↓  flow.bin  [uint16/native I/O, NPU]
       z [1, 192, 1536]
    ↓  decoder.bin × 24 chunks  [uint16/native I/O, NPU]
       audio waveform [float32]
    ↓  soundfile.write
output.wav  (44100 Hz, mono)
```

### Usage

```bat
python melotts_zh.py --text "中文是中国的语言文字，包括汉语和汉字。"
```

Or via the sample launcher from `samples/`:

```bat
python run_inference.py --model melotts_zh --args "--text '中文是中国的语言文字。'"
```

### Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--text` | `"中文是中国的语言文字，包括汉语和汉字。"` | Input Chinese text to synthesize |
| `--out` | `<script_dir>/output.wav` | Output WAV file path |
| `--noise_scale` | `0.667` | Noise scale (controls voice variation) |
| `--noise_scale_w` | `0.8` | Duration noise scale |
| `--length_scale` | `1.0` | Speech rate (>1 is slower, <1 is faster) |
| `--sdp_ratio` | `0.2` | Stochastic duration predictor ratio |

### Model files

The script auto-downloads the QNN context binaries on first run into `../models/`
(a sibling of this `python/` folder). Set `MELOTTS_WORK_DIR` to override the
download location.

| File | I/O dtype | Description |
|------|-----------|-------------|
| `encoder.bin` | float32 | Text encoder — phones + BERT → latent params |
| `flow.bin` | uint16 (native) | Normalizing flow — latent params → `z` |
| `decoder.bin` | uint16 (native) | HiFi-GAN decoder — `z` → audio (chunked, 64 frames/chunk) |

The `.bin` files are **chipset-specific** (Snapdragon X Elite vs X2 Elite).
The script detects the chipset automatically and downloads the correct ZIP.

| Chipset | Download URL |
|---------|-------------|
| Snapdragon X Elite | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/melotts_zh/releases/v0.55.0/melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_x_elite.zip` |
| Snapdragon X2 Elite | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/melotts_zh/releases/v0.56.0/melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_x2_elite.zip` |

MeloTTS hparams config (downloaded separately):
`https://huggingface.co/myshell-ai/MeloTTS-Chinese/resolve/main/config.json`

### Quantization

`flow.bin` and `decoder.bin` use `uint16` quantization. QNN reports the dtype as
`ufp16`, but the bits are actually `uint16`. The script handles the conversion:

- **Quantize** (float32 → uint16 viewed as float16):
  `q = clip(round(x / scale) + zero_point, 0, 65535).astype(uint16).view(float16)`
- **Dequantize** (float16 bits → float32):
  `x = (raw_f16.view(uint16).astype(float32) - zero_point) * scale`

Quantization parameters (from `metadata.json`):

| Tensor | scale | zero_point |
|--------|-------|------------|
| flow input: `attn_squeezed` | 3.0519e-05 | 32768 |
| flow input: `logs_p` | 3.4202e-05 | 22619 |
| flow input: `noise_scale` | 1.0178e-05 | 0 |
| flow input: `m_p` | 7.9627e-05 | 34950 |
| flow input: `y_mask` | 1.5259e-05 | 0 |
| flow input: `g` | 2.0079e-05 | 36547 |
| flow output: `z` | 2.9126e-04 | 32329 |
| decoder input: `g` | 2.0079e-05 | 36547 |
| decoder input: `z` | 2.9233e-04 | 32350 |
| decoder output: `audio` | 1.3070e-05 | 35961 |

### In-process stubs (§1)

The script stubs several native modules that are unavailable or unnecessary on
Windows (ARM64 or x86/AMD64) **before** importing `melo`:

| Stub | Reason |
|------|--------|
| `torchaudio` | No `win_arm64` wheel; not used on the TTS path |
| `numba` | No `win_arm64` wheel (pulled in by librosa); not used |
| `soxr` | No `win_arm64` wheel; only used by `transformers.audio_utils.load_audio` (never called for TTS) |
| `boto3` | Pulled in by `cached_path.schemes.r2`; never actually used |
| `melo.text.korean` | Imported by `melo.text.__init__` even for ZH TTS; Korean path not needed |

### Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_SEQ_LEN` | 512 | Maximum phoneme sequence length |
| `UPSAMPLED_MAX_SEQ_LEN` | 1536 | Maximum upsampled (mel) sequence length |
| `MAX_DEC_SEQ_LEN` | 64 | Decoder chunk size (frames per NPU call) |
| `UPSAMPLE_FACTOR` | 512 | Audio samples per mel frame |
| `SAMPLE_RATE` | 44100 | Output audio sample rate (Hz) |

---

## `setup_env.bat`

One-shot environment setup script. Run once from this folder:

```bat
setup_env.bat
```

Supports both **ARM64** (Windows on Snapdragon) and **x86/AMD64** Python.

### Python auto-detection order

1. **ARM64** (preferred on WoS): `Python313-arm64` → `Python312-arm64` → `Python311-arm64`
   (under `%LOCALAPPDATA%\Programs\Python\` and `C:\`)
2. **x86/AMD64**: `Python313` → `Python312` → `Python311`
   (under `%LOCALAPPDATA%\Programs\Python\`, `C:\`, `C:\Program Files\`)
3. **Fallback**: whatever `python` resolves to on `PATH`

To force a specific interpreter:
```bat
set PYTHON_EXE=C:\Python312\python.exe
setup_env.bat
```

### Steps performed

| Step | Action |
|------|--------|
| 1 | Upgrade `pip` |
| 2 | Install core runtime deps with deps: `qai_appbuilder`, `numpy`, `soundfile`, `requests` |
| 3 | Install `torch` (CPU wheel) from `https://download.pytorch.org/whl/cpu` — used for both ARM64 and x86/AMD64 to avoid CUDA wheels |
| 4 | Download `melotts-0.1.1.tar.gz`, inject empty `requirements.txt`, install with `--no-deps --no-build-isolation` |
| 5 | Install `requirements.txt` with `--no-deps` |
| 6 | Run `patch_melo_japanese.py` |

> ⚠️ Each Python interpreter has its **own** `site-packages`. If you switch
> between ARM64 and x86/AMD64 Python, re-run `setup_env.bat` for the new
> interpreter.

---

## `requirements.txt`

Install with `--no-deps` (as `setup_env.bat` does):

```bat
pip install --no-deps -r requirements.txt
```

**Why `--no-deps`?** MeloTTS' dependency chain pulls heavyweight packages
(`numba`, `llvmlite` via librosa) that have no ARM64 wheel and are not needed
on the TTS path. This file lists only the exact leaf packages required, with
the HuggingFace version triple pinned:

```
transformers==4.56.2
tokenizers==0.22.2
huggingface_hub==0.34.4
```

> Do **not** bump these versions. `transformers 4.56.x` requires
> `tokenizers >=0.22,<0.23` and `huggingface_hub >=0.34,<1.0`. Any other
> combination raises `"X is required for a normal functioning of this module"`
> at `from transformers import AutoTokenizer`.

---

## `patch_melo_japanese.py`

Patches `melo/text/japanese.py` to be import-safe when MeCab and the Japanese
BERT tokenizer are absent. **This patch is required** — without it, `import melo`
fails on any Windows machine that does not have MeCab installed (which is
virtually all of them).

Run standalone (idempotent — safe to run more than once):

```bat
python patch_melo_japanese.py
```

### Three edits applied

| Edit | Original | Patched |
|------|----------|---------|
| 1. MeCab import guard | `raise ImportError(...)` on `ImportError` | `MeCab = None` on any exception |
| 2. `_TAGGER` guard | `_TAGGER = MeCab.Tagger()` | `_TAGGER = MeCab.Tagger() if MeCab is not None else None` |
| 3. AutoTokenizer guard | `tokenizer = AutoTokenizer.from_pretrained(model_id)` | wrapped in `try/except`, sets `tokenizer = None` on failure |

The script prints `[OK]` for each edit applied, `[SKIP]` if already patched,
and `[WARN]` if neither the original nor the patched text is found (version drift).
