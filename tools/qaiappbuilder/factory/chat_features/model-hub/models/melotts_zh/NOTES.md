# MeloTTS-ZH — Model Notes

> ✅ **`infer_melotts_zh.py` is verified end-to-end on Snapdragon X Elite (2026-07) and produces correct Chinese speech. Just run it directly.**
> This file documents **every** dependency / patch / stub you must apply on a fresh WoS ARM64 machine — nothing here is optional.
>
> **Two-layer architecture**: Steps 1–6 build the Python environment (must be done once on every
> fresh machine). The Pipeline / Critical design decisions sections document the *correct*
> inference approach (NPU `bert_wrapper.bin` path), which supersedes the old
> `hfl/chinese-roberta-wwm-ext-large` CPU-BERT approach. Both layers are required on a fresh machine.

---

## Quick Start (verified command sequence — copy verbatim)

`$py = <python_arm64_venv>\Scripts\python.exe`. `<python_arm64_venv>` is a placeholder: on ARM64 hosts (`windows-arm64` / `linux-aarch64`) read it from `python_arm64_venv` in `${APP_ROOT}\data\config\qairt_env.json`; on x64 hosts (`windows-x64` / `linux-x64`) read it from `python_runtime_venv` in the same file. Do not hardcode any particular machine's absolute path. Per-platform inference routing → `${APP_ROOT}/factory/chat_features/_shared/qnn-inference-routing.md`.

### Step 1 — Detect chipset and pick the right ZIP

```powershell
Get-ChildItem "HKLM:\SYSTEM\CurrentControlSet\Services" |
  Where-Object { $_.PSChildName -like "qcadsp*" } |
  Get-ItemProperty | Select-Object PSChildName, ImagePath
```

| `qcadsprpc8380` → X Elite → use `v0.55.0` X Elite ZIP |
| `qcadsprpc8480` → X2 Elite → use `v0.56.0` X2 Elite ZIP |

> ⚠️ **Do NOT run the X2 Elite ZIP on X Elite (or vice versa)** — the `.bin` files are chipset-specific and will produce garbage audio.

### Step 2 — Download & extract the AI Hub package

```powershell
# X Elite
$url = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/melotts_zh/releases/v0.55.0/melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_x_elite.zip"
# X2 Elite (uncomment if needed)
# $url = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/melotts_zh/releases/v0.56.0/melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_x2_elite.zip"
New-Item -ItemType Directory -Force "C:\WoS_AI\melotts_zh" | Out-Null
curl.exe -k -L $url -o "C:\WoS_AI\melotts_zh\melotts_zh.zip"
& $py -c "import zipfile; zipfile.ZipFile(r'C:\WoS_AI\melotts_zh\melotts_zh.zip').extractall(r'C:\WoS_AI\melotts_zh')"
```

ZIP extracts to a subfolder named `melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_x_elite\` (or `x2_elite`). The inference script auto-detects this subfolder by glob — do not rename it.

**ZIP contents** (all required):

| File | Role | Precision |
|------|------|-----------|
| `bert_wrapper.bin` | NPU BERT (bert-base-multilingual-uncased) → `[1,200,768]` | float32 |
| `encoder.bin` | Text encoder (enc_p + SDP + DP) | float32 |
| `flow.bin` | Flow reverse pass (z_p → z), long path | w8a16 |
| `decoder.bin` | HiFi-GAN vocoder, streaming chunks | w8a16 |
| `bert_zh_tokenizer.bin` | Binary BERT vocab for local tokenizer | — |
| `bert_normalizer.bin` | Unicode DAG for BERT text normalization | — |
| `metadata.json` | I/O shapes + quantization params | — |
| `config.json` | QNN runtime metadata (voices, sample rate) | — |

> `flow_short.bin` is **not** present in the v0.55.0 / v0.56.0 AI Hub ZIPs.
> The inference script uses the long-path `flow.bin` only.

### Step 3 — Fetch the MeloTTS hparams config (needed because the default download URLs are dead)

`melo.download_utils` still points at the old `myshell-public-repo-hosting.s3.amazonaws.com/openvoice/basespeakers/ZH/…` URL, which now returns **403 Forbidden**. Get the hparams JSON from HuggingFace instead:

```powershell
curl.exe -k -L "https://huggingface.co/myshell-ai/MeloTTS-Chinese/resolve/main/config.json" -o "C:\WoS_AI\melotts_zh\melo_config_zh.json"
```

This file must contain the keys `train`, `data`, `model`, `symbols`, `num_tones`, `num_languages`. Do **not** confuse it with the `config.json` inside the AI Hub ZIP — that one is QNN runtime metadata (voices, assets, chip version), not melo hparams.

### Step 4 — Install `melotts` from the patched sdist

`pip install melotts` **fails out of the box** because its `setup.py` reads `requirements.txt` but the sdist on PyPI does not ship that file:

```
FileNotFoundError: [Errno 2] No such file or directory:
'…\\melotts_…\\requirements.txt'
```

Workaround — download the tarball, inject an empty `requirements.txt`, install locally without build isolation:

```powershell
$cache = "C:\WoS_AI\melotts_zh\pip_cache"
New-Item -ItemType Directory -Force $cache | Out-Null
curl.exe -k -L "https://files.pythonhosted.org/packages/source/m/melotts/melotts-0.1.1.tar.gz" -o "$cache\melotts-0.1.1.tar.gz"
tar -xzf "$cache\melotts-0.1.1.tar.gz" -C $cache
"" | Out-File -Encoding ascii "$cache\melotts-0.1.1\requirements.txt"
& $py -m pip install --no-deps --no-build-isolation "$cache\melotts-0.1.1"
```

### Step 5 — Install runtime deps (versions matter on ARM64!)

```powershell
# Small helper — everything is --no-deps because the melotts dependency tree
# pulls in build-only / heavyweight packages we do not actually need at runtime.
$np = "--no-deps"

# --- HuggingFace stack: version triple is TIGHTLY coupled on ARM64 ---
# WoS ARM64 has pre-built tokenizers wheels for 0.22.x and 0.23.1 only.
# transformers 4.56.x requires tokenizers >=0.22,<0.23 AND huggingface_hub >=0.34,<1.0.
# ANY other combination triggers "ImportError: X is required for a normal
# functioning of this module" at `from transformers import AutoTokenizer`.
& $py -m pip install $np "transformers==4.56.2" "tokenizers==0.22.2" "huggingface_hub==0.34.4" safetensors regex tqdm

# --- text / phonemizer deps for melo ---
& $py -m pip install $np num2words g2p_en cn2an pykakasi anyascii jamo g2pk2 gruut cached_path jaconv
& $py -m pip install $np lazy_loader scipy audioread pooch scikit-learn msgpack threadpoolctl decorator
& $py -m pip install $np librosa soundfile
& $py -m pip install $np pypinyin jieba unidecode txtsplit narwhals

# --- gruut transitive deps (french/spanish text modules pull gruut at import time) ---
& $py -m pip install $np babel networkx dateparser python-crfsuite tzlocal python_dateutil pytz jsonlines
& $py -m pip install $np gruut-ipa gruut-lang-en gruut-lang-fr gruut-lang-de deprecated wrapt

# --- cached_path transitive deps (imported at module load, not just when used) ---
& $py -m pip install $np google-api-core google-cloud-storage google-cloud-core google-auth google-resumable-media googleapis-common-protos
& $py -m pip install boto3   # WITH deps — cached_path's r2 scheme does `import boto3.session` at load time
```

> 💡 **Do NOT `pip install soxr`** — it has no `win_arm64` wheel and its CMake build fails. `soxr` is only touched by `transformers.audio_utils.load_audio`, which the TTS path never calls. The inference script stubs it in-process (see below).

### Step 6 — Patch `melo/text/japanese.py` (three edits)

```powershell
& $py -c "import melo, os; print(os.path.join(os.path.dirname(melo.__file__), 'text', 'japanese.py'))"
```

Edits (line numbers in melotts 0.1.1):

| # | Location | Original | Replace with |
|---|----------|----------|--------------|
| 1 | line ~11 | `except ImportError as e:\n    raise ImportError(…) from e` | `except Exception:\n    MeCab = None` |
| 2 | line ~570 | `tokenizer = AutoTokenizer.from_pretrained(model_id)` | wrap in `try/except Exception: tokenizer = None` |
| 3 | line ~367 | `_TAGGER = MeCab.Tagger()` | `_TAGGER = MeCab.Tagger() if MeCab is not None else None` |

> ⚠️ **All three edits are required.** Edit #3 is the most commonly missed: with `MeCab = None` from edit #1, the bare `_TAGGER = MeCab.Tagger()` at module level raises `AttributeError: 'NoneType' object has no attribute 'Tagger'` and `from melo.api import TTS` fails.

`melo/text/korean.py` does not need file-level edits — the inference script installs a Python-level stub before `import melo` (see script section 1).

### Step 7 — Run

```powershell
$env:PYTHONUTF8 = "1"
& $py "C:\WoS_AI\melotts_zh\infer_melotts_zh.py"
# Output: C:\WoS_AI\melotts_zh\output.wav  (44100 Hz, ~3.36 s for the default sentence)
```

The full run (bert + encoder + flow + decoder chunks on HTP) takes ~500 ms of NPU compute after models are loaded.

---

## Model Info

> **Path convention**: this note follows the `aihub-model-run` skill's fixed working directory `C:\WoS_AI\<model_name>\` — not a machine-specific absolute path. Everything the script needs (extracted ZIP, `melo_config_zh.json`, `output.wav`) lives under `C:\WoS_AI\melotts_zh\`. To relocate, either set the env var `MELOTTS_WORK_DIR=<your\path>` or edit the single `WORK_DIR` constant at the top of `infer_melotts_zh.py` — the script derives `MODEL_DIR` / `MELO_CONFIG_JSON` / `OUTPUT` from it and auto-detects the chipset-suffixed subfolder (X Elite / X2 Elite / X Plus).

| Item | Value |
|------|-------|
| Format | AI Hub ZIP — 4 NPU `.bin` sub-models + 2 tokenizer `.bin` files |
| Precision | `mixed_with_float` (encoder/bert: float32; flow/decoder: w8a16) |
| Sample rate | 44100 Hz |
| Speaker | `SPEAKER_ID = 1` (only valid ZH speaker; **0 produces wrong prosody**) |
| Download (X Elite, v0.55.0) | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/melotts_zh/releases/v0.55.0/melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_x_elite.zip` |
| Download (X2 Elite, v0.56.0) | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/melotts_zh/releases/v0.56.0/melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_x2_elite.zip` |
| Work dir (default) | `C:\WoS_AI\melotts_zh\` (override with `$env:MELOTTS_WORK_DIR`) |
| Extracted subfolder | auto-detected by prefix `melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_*` |
| Reference implementation | `${APP_ROOT}/factory/chat_features/app-builder/models/melotts-zh/runner.py` |

ZIP contents: `encoder.bin` / `flow.bin` / `decoder.bin` / `bert_wrapper.bin` / `bert_zh_tokenizer.bin` / `bert_normalizer.bin` / `metadata.json` / `config.json`

> ⚠️ Do NOT run the X2 Elite ZIP on X Elite (or vice versa) — the `.bin` files are chipset-specific and will produce garbage audio. Run the chipset detection in **Step 1** to pick the right ZIP.

---

## Pipeline (implemented by infer_melotts_zh.py)

```
text
 └─ CPU G2P: melo_zh_local.clean_text() + cleaned_text_to_sequence()
             → intersperse with blank (0)
             → phone_ids / tone_ids / lang_ids  [len = phone_len]

 └─ NPU bert_wrapper.bin  [float32 I/O]
        input:  input_ids [1,200] int32
                token_type_ids [1,200] int32
                attention_mask [1,200] int32
        output: hidden_states [1,200,768] float32
        → expand by word2ph → ja_bert [768, phone_len]
        → pad to MAX_SEQ_LEN=512 → ja_bert_padded [1,768,512]
        bert_padded [1,1024,512] = zeros  ← ZH_MIX_EN path does NOT use large BERT

 └─ NPU encoder.bin  [float32 I/O]
        inputs (in this exact order):
          sid [1] int32 = 1
          bert [1,1024,512] float32  ← all zeros
          ja_bert [1,768,512] float32
          x [1,512] int32
          tone [1,512] int32
          language [1,512] int32
          x_lengths [1] int32
          noise_scale_w [1] float32
          sdp_ratio [1] float32
          length_scale [1] float32
        outputs:
          m_p [1,192,512], logs_p [1,192,512], w_ceil [1,1,512],
          y_lengths [1] float32, x_mask [1,1,512], g [1,256,1]

 └─ CPU generate_attn_squeezed()  (pure numpy)
        → attn_squeezed [1,1536,512]
        → y_mask [1,1,1536]

 └─ NPU flow.bin  [float32 I/O]
        inputs (in this exact order):
          attn_squeezed [1,1536,512]
          logs_p [1,192,512]
          noise_scale [1] float32 = 0.667
          m_p [1,192,512]
          y_mask [1,1,1536]
          g [1,256,1]
        output: z [1,192,1536]
        → z = z * y_mask  ← MANDATORY mask application after flow

 └─ NPU decoder.bin  [float32 I/O]  — overlap streaming
        z input shape [1,192,T] where T is probed at runtime via getInputShapes()
        T=64  → main_len=40,  overlap=12   ← v0.55.0 / v0.56.0 AI Hub ZIPs
        T=128 → main_len=104, overlap=12
        inputs: probe order with getInputName() — g first OR z first depending on build
        output: audio [1,1,T*512]
        → peak-normalize to 0.95 → PCM-16 WAV

        ⚠️ STREAMING ALGORITHM — copy exactly, do NOT invent crossfade/overlap-add:
        The overlap frames are context fed to the decoder; their decoded audio is
        DISCARDED — never mixed/blended with adjacent chunks.

        ```python
        UPSAMPLE = 512
        # Chunk 0: z[0 : main_len+overlap] → keep audio[0 : main_len*512]
        z_buf = np.zeros((1, 192, T), dtype=np.float32)
        z_buf[:, :, :main_len+overlap] = z[:, :, :main_len+overlap]
        audio_raw = decoder.Inference([g, z_buf])[0].reshape(-1)  # g first for AI Hub ZIP
        audio = audio_raw[: main_len * UPSAMPLE]
        total = main_len
        # Chunk k (k≥1): z[total-overlap : total+main_len+overlap]
        while total < y_lengths_int:
            start = total - overlap
            end   = total + main_len + overlap
            z_slice = z[:, :, start:end]          # variable width ≤ T
            z_buf = np.zeros((1, 192, T), dtype=np.float32)
            z_buf[:, :, :z_slice.shape[2]] = z_slice
            audio_raw = decoder.Inference([g, z_buf])[0].reshape(-1)
            # Discard leading overlap, keep central main_len frames only
            audio = np.concatenate([audio,
                audio_raw[overlap*UPSAMPLE : (main_len+overlap)*UPSAMPLE]])
            total += main_len
        audio = audio[: y_lengths_int * UPSAMPLE]  # trim to exact length
        ```
```

> ⚠️ **The old approach** (`melo.api.TTS` + `hfl/chinese-roberta-wwm-ext-large` CPU BERT) is
> still used by the in-script stubs infrastructure (Steps 3–6) to populate `tts_obj.hps` and
> `tts_obj.symbol_to_id`, but **all NPU inference uses `bert_wrapper.bin`** — never the large
> HF BERT model. Do not pass `hfl/chinese-roberta-wwm-ext-large` features to the encoder.

---

## Critical design decisions (must get all of these right)

### 1. All sub-models use `DataType.FLOAT` I/O

```python
QNNContext("name", "file.bin",
           input_data_type=DataType.FLOAT,
           output_data_type=DataType.FLOAT)
```

This applies to **all four** sub-models including flow and decoder (which are w8a16 internally).
QNN handles the float↔uint16 conversion automatically.

**Do NOT** use `output_data_type="native"` for these models. On this platform it returns a
**float16 container wrapping uint16 values (ufp16)** — large uint16 values fall in the float16
NaN bit-range, so the output appears to be all-NaN.

> Note: the Encoder I/O / Flow I/O / Decoder I/O sections below document the raw quantization
> params for reference, but the current script uses `DataType.FLOAT` and does not need them.

### 2. BERT: use `bert_wrapper.bin` on NPU — never `hfl/chinese-roberta-wwm-ext-large` directly

- `ja_bert [1,768,512]` ← from `bert_wrapper.bin` output, expanded by `word2ph`
- `bert [1,1024,512]`   ← **always zeros**; ZH_MIX_EN path does not use the large BERT

Using `hfl/chinese-roberta-wwm-ext-large` features directly produces noisy audio (RMS < 0.01).

### 3. SPEAKER_ID = 1

```python
sid_np = np.array([1], dtype=np.int32)
```

Speaker 0 is a different (non-ZH) voice. Using 0 produces wrong prosody or noise.

### 4. `z = z * y_mask` after flow

```python
z = np.asarray(flow_out[0], dtype=np.float32)
z = z * y_mask_np   # mandatory; zeros out frames beyond y_lengths
```

### 5. Decoder T dimension: probe at runtime, do not hardcode

The AI Hub v0.55.0 / v0.56.0 ZIPs ship `decoder.bin` with z input `[1,192,64]`.
The App Builder `runner.py` uses `DECODER_Z_TIME_DIM=128` for a different decoder variant.

```python
DECODER_T_PRESETS = {64:(40,12), 128:(104,12), 192:(168,12), 256:(232,12)}
_dec_z_shape = dec_ctx.getInputShapes()[1]  # z is input index 1 (after g)
T = _dec_z_shape[2]                          # 64 for AI Hub ZIPs
main_len, overlap = DECODER_T_PRESETS.get(T, (T - 24, 12))
```

### 6. Decoder streaming: NO crossfade — discard overlap audio, do NOT blend

> 🚨 **This is the most common implementation mistake.** The overlap frames give the decoder
> causal context; the audio they produce is **discarded**, not mixed with adjacent chunks.
> Any crossfade / overlap-add / linear-fade implementation produces audible echo/reverb.

Correct algorithm (verbatim from `runner.py::_streaming_decode`):
- **Chunk 0**: feed `z[:, :, 0 : main_len+overlap]` → keep `audio[0 : main_len*512]`
- **Chunk k (k≥1)**: feed `z[:, :, total-overlap : total+main_len+overlap]` → keep `audio[overlap*512 : (main_len+overlap)*512]` — the leading `overlap*512` samples are thrown away
- **Final**: `audio = audio[:y_lengths*512]`

See the full copyable code block in the Pipeline section above (under `NPU decoder.bin`).

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel, ProfilingLevel, PerfProfile
QNNConfig.Config(runtime=Runtime.HTP, log_level=LogLevel.WARN, profiling_level=ProfilingLevel.BASIC)
# use keyword args — positional call can silently shift args on some qai_appbuilder builds

# After all QNNContext objects are created, hold BURST across all inference, release at end:
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
try:
    # bert → encoder → flow → decoder loop
finally:
    PerfProfile.RelPerfProfileGlobal()
```

---

## In-script stubs & monkey-patches (must run BEFORE `import melo`)

Full copyable stub code for all ARM64 missing packages is in
`references/known-issues.md` **Issue 6**: torchaudio / numba (`_NumbaType` with both
`__getitem__` and `__call__`) / soxr / boto3 / korean (`get_bert_feature` attribute required) /
`melo.download_utils` replacement / `SynthesizerTrn.load_state_dict` relaxation.

melotts_zh-specific note: `melo.download_utils` replacement uses
`MELO_CONFIG_JSON = os.path.join(WORK_DIR, "melo_config_zh.json")` (from Step 3).

For SSL / HuggingFace network issues see `references/known-issues.md` **Issue 19**.
> ⚠️ SSL workaround disables HTTPS verification process-wide — do not commit to default script.

---

## Encoder I/O (float32, verified)

Load: `QNNContext("encoder", "encoder.bin", input_data_type=DataType.FLOAT, output_data_type=DataType.FLOAT)`

**Inputs** (strictly in this order):

| # | Name | Shape | dtype |
|---|------|-------|-------|
| 0 | sid | [1] | int32 |
| 1 | bert | [1,1024,512] | float32 |
| 2 | ja_bert | [1,768,512] | float32 |
| 3 | x | [1,512] | int32 |
| 4 | tone | [1,512] | int32 |
| 5 | language | [1,512] | int32 |
| 6 | x_lengths | [1] | int32 |
| 7 | noise_scale_w | [1] | float32 |
| 8 | sdp_ratio | [1] | float32 |
| 9 | length_scale | [1] | float32 |

**Outputs** (by index): `[0]m_p[1,192,512]`  `[1]logs_p[1,192,512]`  `[2]w_ceil[1,1,512]`  `[3]y_lengths[1]`  `[4]x_mask[1,1,512]`  `[5]g[1,256,1]`

> `g` uses encoder output [5] directly, no need to load it from PyTorch weights.
> `y_lengths` is float32, use `int(y_lengths[0])`.
>
> Flow / Decoder quantization params (scale, zero_point) are in `metadata.json` inside the
> extracted ZIP. Current script uses `DataType.FLOAT` so these are not needed at runtime.

---

## generate_attn_squeezed (CPU, pure numpy)

```python
import numpy as np

def build_y_mask(y_lengths, max_len=1536):
    return (np.arange(max_len, dtype=np.int64) < y_lengths).astype(np.float32).reshape(1, 1, -1)

def generate_attn_squeezed(w_ceil, x_mask, y_mask):
    # w_ceil: [1,1,512]  x_mask: [1,1,512]  y_mask: [1,1,1536]
    attn_mask = x_mask[:, :, np.newaxis, :] * y_mask[:, :, :, np.newaxis]  # [1,1,1536,512]
    b, _, t_y, t_x = attn_mask.shape
    cum = np.cumsum(w_ceil, axis=-1).reshape(b * t_x)
    path = (np.arange(t_y, dtype=cum.dtype)[None, :] < cum[:, None]).astype(attn_mask.dtype)
    path = path.reshape(b, t_x, t_y)
    path = path - np.pad(path, ((0,0),(1,0),(0,0)))[:, :-1]
    attn = path[:, np.newaxis, :, :].transpose(0, 1, 3, 2) * attn_mask
    return attn.squeeze(1).astype(np.float32)  # [1,1536,512]

# Call:
y_lengths_int = int(np.asarray(enc_out[3]).flat[0])
y_mask_np = build_y_mask(y_lengths_int)                       # [1,1,1536]
attn_squeezed = generate_attn_squeezed(w_ceil, x_mask, y_mask_np)  # [1,1536,512]
```

---

## Verified Output

Key metrics for a normal run (sentence: `"今天天气真不错，我们一起去公园散步吧。"`):

```
[PRE]  phone_len=77           # normal 10~200
[TTS]  y_lengths=289          # normal 100~1500
[TTS]  m_p=[-2.7, 2.1]        # normal [-5,5]
[TTS]  z range=[-8.9,8.4] mean≈0   # ✅ mean must be close to 0; large mean → wrong BERT
[TTS]  audio rms≈0.14              # ✅ after peak-normalize to 0.95
[TTS]  duration≈3.36 s
[TTS]  NPU time≈500 ms total (bert ~12ms, enc ~60ms, flow ~145ms, dec×8 ~90ms each)
```

| Abnormal symptom | Cause |
|----------|------|
| z range shows 16248 or mean > 100 | flow quantization/dequantization error (most common! see SKILL.md Issue 5); or `output_data_type="native"` ufp16 trap |
| z normal but audio all noise | decoder quantization params wrong or g/z input order wrong |
| y_lengths=0 or >1536 | encoder input error (bert dimension order?) |
| audio range ≈ 0 | decoder output dequantization error |
| audio too short or scrambled | hardcoded T=128 but decoder.bin is T=64; probe `getInputShapes()` at runtime |
| **echo / reverb / double-voice** | **decoder streaming used crossfade/overlap-add instead of simple slice; overlap audio must be DISCARDED, never mixed — see Critical decision #6** |
| artifacts / noise at end | missing `z = z * y_mask` after flow |
| phone_len=0 | G2P failed, check melo dependencies |
| wrong prosody / wrong voice | SPEAKER_ID=0; set to 1 |
| import errors on MeCab / AutoTokenizer / soxr / boto3 / `_TAGGER` | japanese.py not fully patched (all 3 edits!) or in-script stubs missing |
| `ImportError: X is required for a normal functioning of this module` (huggingface_hub / tokenizers) | wrong transformers/tokenizers/huggingface_hub triple — see Step 5 |
| `FileNotFoundError: … melotts_… / requirements.txt` | tried to `pip install melotts` directly instead of the sdist workaround in Step 4 |
| `403 Client Error … myshell-public-repo-hosting.s3…` at `TTS("ZH")` | the `melo.download_utils` monkey-patch did not run before `import melo.api` |
| `Missing key(s) in state_dict …` at `TTS("ZH")` | `SynthesizerTrn.load_state_dict` was not relaxed to `strict=False` |

---

## Troubleshooting matrix — what breaks and where to fix it

| Error (typical) | Root cause | Fix (this file's section) |
|-----------------|------------|---------------------------|
| `FileNotFoundError … melotts_… / requirements.txt` during `pip install melotts` | sdist ships `setup.py` that reads a missing `requirements.txt` | Step 4 |
| `ImportError: tokenizers>=0.22,<=0.23 required, but found 0.23.1` (or similar) | transformers ↔ tokenizers ↔ huggingface_hub version mismatch on ARM64 | Step 5 (pin the triple) |
| `ImportError: huggingface-hub>=0.34,<1.0 required, but found 1.23.0` | huggingface_hub 1.x installed by another package | Step 5 (`huggingface_hub==0.34.4`) |
| `ModuleNotFoundError: No module named 'soxr'` from `transformers.audio_utils` | soxr has no `win_arm64` wheel; transformers ≥ 4.55 imports it at module top | `references/known-issues.md` Issue 6 (soxr stub) |
| `ModuleNotFoundError: No module named 'boto3.session'` from `cached_path.schemes.r2` | cached_path imports r2 unconditionally | install real `boto3` **with deps** (Step 5) or Issue 6 (boto3 stub) |
| `ModuleNotFoundError: pytz / jsonlines / babel / dateparser / …` | gruut transitive deps needed at import of `melo.text.french` | Step 5 (gruut deps block) |
| `AttributeError: 'NoneType' object has no attribute 'Tagger'` at `_TAGGER = MeCab.Tagger()` | japanese.py edit #3 skipped | Step 6 (all three edits) |
| `ImportError: cannot import name 'get_bert_feature' from 'melo.text.korean'` | korean stub missing the `get_bert_feature` attribute | `references/known-issues.md` Issue 6 (korean stub) |
| `TypeError: 'NoneType' object is not callable` at `numba.void(…)` | `_NumbaType` missing `__call__`, or `void` set to `None` | `references/known-issues.md` Issue 6 (numba mock) |
| `403 Forbidden … myshell-public-repo-hosting.s3…` at `TTS("ZH")` | `melo.download_utils` URL is dead | Step 3 + `references/known-issues.md` Issue 6 (monkey-patch **before** `import melo.api`) |
| `SSLError … CA cert does not include key usage extension` or timeouts to huggingface.co | corporate / broken WoS CA store, or network-restricted region | `references/known-issues.md` Issue 19 (do NOT enable by default) |
| `RuntimeError: Error(s) in loading state_dict for SynthesizerTrn: Missing key(s) …` | dummy checkpoint + `strict=True` | `references/known-issues.md` Issue 6 (load_state_dict relaxation) |
