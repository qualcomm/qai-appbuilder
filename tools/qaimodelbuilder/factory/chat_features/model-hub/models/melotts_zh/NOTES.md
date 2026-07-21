# MeloTTS-ZH — Model Notes

> ✅ **`infer_melotts_zh.py` is verified end-to-end on Snapdragon X Elite (2026-07) and produces normal speech. Just run it directly.**
> Common issues (uint16 trap, torchaudio mock, QNNConfig, Inference order) are in `SKILL.md`.
> This file documents **every** dependency / patch / stub you must apply on a fresh WoS ARM64 machine — nothing here is optional.

---

## Quick Start (verified command sequence — copy verbatim)

`$py = <python_arm64_venv>\Scripts\python.exe` (read `python_arm64_venv` from `${APP_ROOT}\data\config\qairt_env.json`; do not hardcode any particular machine's absolute path).

### Step 1 — Detect chipset and pick the right ZIP

```powershell
Get-ChildItem "HKLM:\SYSTEM\CurrentControlSet\Services" |
  Where-Object { $_.PSChildName -like "qcadsp*" } |
  Get-ItemProperty | Select-Object PSChildName, ImagePath
```

| `qcadsprpc8380` → X Elite → use `v0.55.0` X Elite ZIP |
| `qcadsprpc8480` → X2 Elite → use `v0.56.0` X2 Elite ZIP |

> ⚠️ **Do NOT run the X2 Elite ZIP on X Elite (or vice versa)** — the `.bin` files are chipset-specific and will produce garbage audio.

### Step 2 — Download & extract the VOICE_AI package

```powershell
# X Elite (adjust URL / output dir for X2 Elite as noted below)
$url = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/melotts_zh/releases/v0.55.0/melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_x_elite.zip"
New-Item -ItemType Directory -Force "C:\WoS_AI\melotts_zh" | Out-Null
curl.exe -k -L $url -o "C:\WoS_AI\melotts_zh\melotts_zh.zip"
& $py -c "import zipfile; zipfile.ZipFile(r'C:\WoS_AI\melotts_zh\melotts_zh.zip').extractall(r'C:\WoS_AI\melotts_zh')"
```

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

| # | Original | Replace with |
|---|----------|--------------|
| 1 (line ~11) | `try:\n    import MeCab\nexcept ImportError as e:\n    raise ImportError(…) from e` | `try:\n    import MeCab\nexcept Exception:\n    MeCab = None` |
| 2 (line ~570) | `tokenizer = AutoTokenizer.from_pretrained(model_id)` | `try:\n    tokenizer = AutoTokenizer.from_pretrained(model_id)\nexcept Exception:\n    tokenizer = None` |
| 3 (line ~367) | `_TAGGER = MeCab.Tagger()` | `_TAGGER = MeCab.Tagger() if MeCab is not None else None` |

> ⚠️ **Edit #3 was missing from the previous version of this note.** With `MeCab = None`, the module-level `_TAGGER = MeCab.Tagger()` at line 367 raises `AttributeError: 'NoneType' object has no attribute 'Tagger'` and `from melo.api import TTS` fails. All three edits are required.

`melo/text/korean.py` does not need file-level edits — the inference script installs a Python-level stub before `import melo` (see script section 1).

### Step 7 — Run

```powershell
$env:PYTHONUTF8 = "1"
& $py "${APP_ROOT}\skills\aihub-model-run\models\melotts_zh\infer_melotts_zh.py"
# Output: C:\WoS_AI\melotts_zh\output.wav  (44100 Hz, ~3.36 s for the default sentence)
```

The full run (encoder + flow + 24 × decoder chunks on HTP) takes ~3 s of NPU compute after models are loaded; each decoder chunk is ~86-93 ms.

---

## Model Info

> **Path convention**: this note follows the `aihub-model-run` skill's fixed working directory `C:\WoS_AI\<model_name>\` — not a machine-specific absolute path. Everything the script needs (extracted ZIP, `melo_config_zh.json`, `output.wav`) lives under `C:\WoS_AI\melotts_zh\`. To relocate, either set the env var `MELOTTS_WORK_DIR=<your\path>` or edit the single `WORK_DIR` constant at the top of `infer_melotts_zh.py` — the script derives `MODEL_DIR` / `MELO_CONFIG_JSON` / `OUTPUT` from it and auto-detects the chipset-suffixed subfolder (X Elite / X2 Elite / X Plus).

| Item | Value |
|------|-------|
| Format | VOICE_AI — multiple sub-models in one ZIP |
| Precision | `mixed_with_float` |
| Sample rate | 44100 Hz |
| Download (X Elite, v0.55.0) | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/melotts_zh/releases/v0.55.0/melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_x_elite.zip` |
| Download (X2 Elite, v0.56.0) | `https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/melotts_zh/releases/v0.56.0/melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_x2_elite.zip` |
| Work dir (default) | `C:\WoS_AI\melotts_zh\` (override with `$env:MELOTTS_WORK_DIR`) |
| Extracted subfolder | auto-detected by prefix `melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_*` |

ZIP contents: `encoder.bin` / `flow.bin` / `decoder.bin` / `bert_wrapper.bin` / `bert_zh_tokenizer.bin` / `bert_normalizer.bin` / `metadata.json` / `config.json`

> ⚠️ Do NOT run the X2 Elite ZIP on X Elite (or vice versa) — the `.bin` files are chipset-specific and will produce garbage audio. Run the chipset detection in **Step 1** to pick the right ZIP.

---

## Pipeline (implemented by infer_melotts_zh.py)

```
text → melo.api.TTS("ZH") + get_text_for_tts_infer()   [CPU]
     → bert_feat[1024,L], ja_bert_feat[768,L], phones/tones/lang_ids
     → encoder.bin   [float32 I/O, NPU]
     → m_p[1,192,512], logs_p[1,192,512], w_ceil[1,1,512], y_lengths[1], x_mask[1,1,512], g[1,256,1]
     → generate_path()   [CPU]  → attn_squeezed[1,1536,512]
     → flow.bin   [uint16/native I/O, NPU]  → z[1,192,1536]
     → decoder.bin × 24 chunks   [uint16/native I/O, NPU]  z[1,192,64] → audio[1,1,32768]
     → concat + trim → WAV
```

> ⚠️ **`bert_wrapper.bin` is NOT called**. `get_text_for_tts_infer()` (running on CPU with the `hfl/chinese-roberta-wwm-ext-large` HF model) already produces the BERT features.
> `runner.py` (the App Builder version) calls bert_wrapper.bin, which is **incompatible with this approach — do not mix them**.

---

## In-script stubs & monkey-patches (must run BEFORE `import melo`)

The inference script does the following in-process; **understanding them is essential when adapting the script**:

### 1. Native modules with no ARM64 wheel — replace with stubs

| Module | Why | Stub form |
|--------|-----|-----------|
| `torchaudio` | `melo.api` imports it at module top; not used on the TTS path. | Empty `types.ModuleType` with `__spec__` and `__version__`. |
| `numba` | `melo/monotonic_align/core.py` uses `@numba.jit` + type-index syntax `numba.int32[:, ::1]` AND calls `numba.void(...)` as a function. | `_NumbaType` class with **both** `__getitem__` and `__call__`; `jit` is a no-op decorator; `void`/`int32`/`int64`/`float32`/`float64` all bound to a `_NumbaType()` instance (see below). |
| `soxr` | `transformers.audio_utils` imports it at module top (transformers ≥ 4.55); never called on the TTS path. | Empty module with `__version__`, a `resample` that raises, and dummy quality constants `VHQ/HQ/MQ/LQ/QQ`. |
| `boto3` | `cached_path.schemes.r2` does `import boto3.session` at module top; melo.download_utils imports cached_path unconditionally. | Either install the real `boto3` (with deps) **or** stub `boto3`, `boto3.session`, `boto3.dynamodb` as empty modules. |
| `melo.text.korean` | `melo/text/__init__.py` executes `from .korean import get_bert_feature`; the real module hard-imports `python-mecab-ko` which does not build on ARM64. | Empty module that exposes a `get_bert_feature(*a, **kw)` raiser. **Setting only `sys.modules['melo.text.korean']` without the function attribute produces `ImportError: cannot import name 'get_bert_feature'`.** |

### 2. `_NumbaType` — both indexable AND callable

```python
class _NumbaType:
    def __getitem__(self, _key): return self   # numba.int32[:, ::1]
    def __call__(self, *a, **kw): return self  # numba.void(numba.int32[:, :, ::1], ...)
_nb_type = _NumbaType()
_numba.void = _numba.int32 = _numba.int64 = _numba.float32 = _numba.float64 = _nb_type
```

### 3. `melo.download_utils` replacement (dead URLs)

The upstream table `DOWNLOAD_CONFIG_URLS` / `DOWNLOAD_CKPT_URLS` points at `https://myshell-public-repo-hosting.s3.amazonaws.com/openvoice/basespeakers/ZH/…`, which now returns **403 Forbidden** (bucket ACL changed). The script installs a replacement module into `sys.modules['melo.download_utils']` **before** `from melo.api import TTS`:

```python
# WORK_DIR defaults to C:\WoS_AI\melotts_zh (override with $env:MELOTTS_WORK_DIR).
MELO_CONFIG_JSON = os.path.join(WORK_DIR, "melo_config_zh.json")   # from Step 3
_dl = types.ModuleType("melo.download_utils")
def _load_or_download_config(locale):
    from melo import utils as _mu
    return _mu.get_hparams_from_file(MELO_CONFIG_JSON)
def _load_or_download_model(locale, device):
    return {"model": {}}   # dummy; real weights live in the QNN .bin files
_dl.load_or_download_config = _load_or_download_config
_dl.load_or_download_model  = _load_or_download_model
sys.modules["melo.download_utils"] = _dl
```

### 4. Relax `SynthesizerTrn.load_state_dict`

Because `_load_or_download_model` returns an empty dict, the default `strict=True` load fails with hundreds of missing keys. The CPU torch model is **never used for inference** — it only exists so `tts_obj.hps` and `tts_obj.symbol_to_id` are populated for `get_text_for_tts_infer()`. Force non-strict loading:

```python
import melo.models as _mm
_orig = _mm.SynthesizerTrn.load_state_dict
def _lax(self, sd, strict=True, **kw): return _orig(self, sd, strict=False, **kw)
_mm.SynthesizerTrn.load_state_dict = _lax
```

### 5. Corporate / restricted-network SSL & HF access (NOT enabled by default)

The stock script talks to HuggingFace normally: `melo.text.chinese_bert.get_bert_feature` downloads `hfl/chinese-roberta-wwm-ext-large` (~1.3 GB) from `huggingface.co` on first run, and every `AutoTokenizer.from_pretrained(...)` in `melo.text.{english,french,spanish,chinese_mix,...}` fetches a few-MB tokenizer. **On a normal home / cloud machine this just works** — do not change anything.

Add the workarounds below **only if** you actually see one of these on this specific machine:

- `requests.exceptions.SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: CA cert does not include key usage extension` (typical on some corporate-managed or older WoS images whose system CA store is broken)
- Long timeouts / `Max retries exceeded` when reaching `huggingface.co` (network-restricted region)

Insert this block **at the very top of `infer_melotts_zh.py`, before any other import**:

```python
import os
# Corporate CA store is broken — disable HTTPS verification for this process.
os.environ["CURL_CA_BUNDLE"] = ""
os.environ["REQUESTS_CA_BUNDLE"] = ""
import ssl
ssl._create_default_https_context = ssl._create_unverified_context
import urllib3; urllib3.disable_warnings()
import requests
_orig = requests.Session.request
def _no_verify(self, *a, **kw):
    kw.setdefault("verify", False)
    return _orig(self, *a, **kw)
requests.Session.request = _no_verify

# Optional: only if huggingface.co itself is unreachable from this machine.
# os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

# Optional: disable the experimental Xet transport (falls back to standard LFS).
# os.environ["HF_HUB_DISABLE_XET"] = "1"
```

> ⚠️ These lines disable HTTPS verification **process-wide** — not just for HF. Keep them out of the default script; add them only on machines that need them. When you commit this workaround, **do not** re-enable it for everyone else.

---

## Encoder I/O (float32, verified)

Load: `QNNContext("encoder", "encoder.bin", input_data_type="float", output_data_type="float")`

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

### bert_feat dimension (error-prone!)

`get_text_for_tts_infer()` returns `bert_feat[1024, phone_len]` (**the first dim is the feature dim**):

```python
# ✅ Correct: pad the last dim (phone_len → 512)
def pad2d(t, n):
    return F.pad(t, (0, max(0, n - t.size(1))))[:, :n]
bert = pad2d(bert_feat, 512).unsqueeze(0).numpy()  # [1,1024,512]

# ❌ Wrong: F.pad(bert_feat.T, ...) or treating phone_len as the first dim
```

---

## Flow I/O (uint16/native, verified)

Load: `QNNContext("flow", "flow.bin", input_data_type="native", output_data_type="native")`

For quantization/dequantization see `SKILL.md` Issue 5.

**Input order + quantization params**:

| # | Name | Shape | scale | zero_point |
|---|------|-------|-------|------------|
| 0 | attn_squeezed | [1,1536,512] | 3.0518509447574615e-05 | 32768 |
| 1 | logs_p | [1,192,512] | 3.420173015911132e-05 | 22619 |
| 2 | noise_scale | [1] | 1.0177767762797885e-05 | 0 |
| 3 | m_p | [1,192,512] | 7.962718518683687e-05 | 34950 |
| 4 | y_mask | [1,1,1536] | 1.5259021893143654e-05 | 0 |
| 5 | g | [1,256,1] | 2.0079150999663398e-05 | 36547 |

**Output**: `z[1,192,1536]`  scale=2.9126188019290566e-04  zero_point=32329

---

## Decoder I/O (uint16/native, verified)

Load: `QNNContext("decoder", "decoder.bin", input_data_type="native", output_data_type="native")`

**Input order + quantization params**:

| # | Name | Shape | scale | zero_point |
|---|------|-------|-------|------------|
| 0 | g | [1,256,1] | 2.0079150999663398e-05 | 36547 |
| 1 | z | [1,192,64] | 2.923276915680617e-04 | 32350 |

**Output**: `audio[1,1,32768]`  scale=1.3070309250906575e-05  zero_point=35961

**Chunking**: z[1,192,1536] is split into 24 chunks of 64 frames; the last chunk with fewer than 64 frames is zero-padded; final trim:
```python
audio = np.concatenate(chunks)[:y_len_int * 512]  # UPSAMPLE_FACTOR=512
```

---

## generate_path (CPU, must implement yourself)

```python
import torch, torch.nn.functional as F

def generate_path(duration, mask):
    """duration:[B,1,Tx], mask:[B,1,Ty,Tx] → attn:[B,1,Ty,Tx]"""
    b, _, t_y, t_x = mask.shape
    cum = torch.cumsum(duration, -1).view(b * t_x)
    path = (torch.arange(t_y, dtype=cum.dtype).unsqueeze(0) < cum.unsqueeze(1)).to(mask.dtype)
    path = path.view(b, t_x, t_y)
    path = path - F.pad(path, [0,0,1,0,0,0])[:, :-1]
    return path.unsqueeze(1).transpose(2, 3) * mask

# Call
y_mask_np = (torch.arange(1536) < torch.tensor([y_len_int]).unsqueeze(-1)).float().unsqueeze(0).numpy()
attn_mask = torch.from_numpy(x_mask).unsqueeze(2) * torch.from_numpy(y_mask_np).unsqueeze(-1)
attn_squeezed = generate_path(torch.from_numpy(w_ceil), attn_mask).squeeze(1).numpy()  # [1,1536,512]
```

---

## QNNConfig initialization (must be the first step)

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel, ProfilingLevel, PerfProfile
QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
# ↑ positional args, the first is "" (empty string = auto-find lib), not the keyword-argument form
```

Optional performance boost (raise the HTP clock **once**, hold it across encoder → flow → decoder, release after the last stage — do NOT Set/Rel per stage):

```python
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
try:
    # encoder → flow → decoder loop …
finally:
    PerfProfile.RelPerfProfileGlobal()
```

---

## Verified Output

Key metrics for a normal run:

```
[PRE]  phone_len=37           # normal 10~200
[TTS]  y_lengths=289          # normal 100~1500
[TTS]  m_p=[-2.x, 2.x]       # normal [-5,5]
[TTS]  z range=[-8.8,9.2] mean≈0   # ✅ mean must be close to 0
[TTS]  audio range=[-0.34,0.25]    # ✅ normal amplitude
```

| Abnormal symptom | Cause |
|----------|------|
| z range shows 16248 or mean > 100 | flow quantization/dequantization error (most common! see SKILL.md Issue 5) |
| z normal but audio all noise | decoder quantization params wrong or g/z input order wrong |
| y_lengths=0 or >1536 | encoder input error (bert dimension order?) |
| audio range ≈ 0 | decoder output dequantization error |
| phone_len=0 | G2P failed, check melo dependencies |
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
| `ModuleNotFoundError: No module named 'soxr'` from `transformers.audio_utils` | soxr has no `win_arm64` wheel; transformers ≥ 4.55 imports it at module top | in-script `soxr` stub (script §1) |
| `ModuleNotFoundError: No module named 'boto3.session'` from `cached_path.schemes.r2` | cached_path imports r2 unconditionally | install real `boto3` **with deps** (Step 5) or in-script `boto3` stub |
| `ModuleNotFoundError: pytz / jsonlines / babel / dateparser / …` | gruut transitive deps needed at import of `melo.text.french` | Step 5 (gruut deps block) |
| `AttributeError: 'NoneType' object has no attribute 'Tagger'` at `_TAGGER = MeCab.Tagger()` | japanese.py edit #3 skipped | Step 6 (all three edits) |
| `ImportError: cannot import name 'get_bert_feature' from 'melo.text.korean'` | korean stub missing the `get_bert_feature` attribute | script §1 (korean stub must expose the function) |
| `TypeError: 'NoneType' object is not callable` at `numba.void(…)` | `_NumbaType` missing `__call__`, or `void` set to `None` | script §2 |
| `403 Forbidden … myshell-public-repo-hosting.s3…` at `TTS("ZH")` | `melo.download_utils` URL is dead | Step 3 + script §3 (monkey-patch **before** `import melo.api`) |
| `SSLError … CA cert does not include key usage extension` or timeouts to huggingface.co | corporate / broken WoS CA store, or network-restricted region | Optional workaround in §5 (do NOT enable by default) |
| `RuntimeError: Error(s) in loading state_dict for SynthesizerTrn: Missing key(s) …` | dummy checkpoint + `strict=True` | script §4 |
