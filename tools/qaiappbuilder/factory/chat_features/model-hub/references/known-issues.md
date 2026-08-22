# Model Hub — Known Issues (Reference)

> REFERENCE read on demand by `SKILL.md` when a symptom matches.
> `SKILL.md` keeps a compact index (symptom → Issue N → this file).

## ⚠️ Known Issues (All Models)

---

### Issue 1: ZIP extraction fails

**Symptom:** `tar: This does not look like a tar archive`
**Cause:** Using `tar -xf` on a `.zip`.
**Fix:** Use Python `zipfile`:
```python
import zipfile; zipfile.ZipFile("model.zip").extractall("out/")
```

---

### Issue 2: Unix tools not found in exec

**Symptom:** `[hint] Detected Unix tool ls. Possible cause: PortableGit not installed`
**Fix:** Specify `shell='sh'`:
```python
exec("ls C:/WoS_AI/", shell='sh')
exec("grep -r pattern dir/", shell='sh')
```
PortableGit is at `%LOCALAPPDATA%\QAIModelBuilder\git\`, available under `shell='sh'`.

> ⚠️ **Do not use `dir | find /c`** — PortableGit's Unix `find` intercepts it. Use PowerShell `(Get-ChildItem <path>\*.raw).Count` or Python `glob`.

---

### Issue 3: Download timeout

**Symptom:** `[process killed: timeout after 30.0s]`
**Fix:** Use `timeout=300` or omit (0 = no limit):
```python
exec('curl -k -L "<url>" -o "out.zip" --create-dirs', timeout=300)  # -k: disable SSL verify (see Issue 19)
```

---

### Issue 4: Chinese print causes UnicodeEncodeError

**Symptom:** `UnicodeEncodeError: 'charmap' codec can't encode characters`
**Fix:**
```python
import sys; sys.stdout.reconfigure(encoding="utf-8", errors="replace")
```
> Use ASCII for log symbols (`->` for `→`, `[OK]` for `✓`) to avoid terminal encoding issues.

---

### Issue 5: QNN native dtype trap (uint16 reported as ufp16)

**Symptom:** NaN or absurd output values.
**Cause:** With `output_data_type='native'`, QNN wraps quantized `uint16` in a float16 container reporting it as `ufp16`.
**Detection:** `"dtype":"uint16"` + `"quantization_parameters"` in `metadata.json`.
**scale/zero_point MUST be taken from `metadata.json`.**

```python
def quantize(x, scale, zp):   # float32 → uint16 viewed as float16
    return np.clip(np.round(x / scale) + zp, 0, 65535).astype(np.uint16).view(np.float16)

def dequantize(raw, scale, zp):  # QNN native out → float32
    return (raw.view(np.uint16).astype(np.float32) - zp) * scale

model = QNNContext("m", "m.bin", input_data_type="native", output_data_type="native")
out = dequantize(np.array(model.Inference([quantize(x, scale=1e-4, zp=32768)])[0]),
                 scale=3e-4, zp=32329)
```

---

### Issue 6: ARM64 Windows missing native packages (torchaudio / numba / soxr / boto3 / MeCab)

All mocks must be placed before the model import.

**torchaudio mock:**
```python
import sys, types, importlib.util
_ta = types.ModuleType("torchaudio")
_ta.__spec__ = importlib.util.spec_from_loader("torchaudio", loader=None)
_ta.__version__ = "0.0.0"
sys.modules.setdefault("torchaudio", _ta)
```

**numba mock:**
```python
_nb = types.ModuleType("numba")
def _jit(*a, **kw): return (lambda f: f) if not (len(a)==1 and callable(a[0])) else a[0]
# ⚠️ void/int32/float32 must support BOTH __getitem__ (numba.int32[:, ::1]) AND __call__
# (numba.void(numba.int32[:, :, ::1], ...)). Setting to None or a plain type breaks either.
class _NumbaType:
    def __getitem__(self, _key): return self
    def __call__(self, *a, **kw): return self
_nb_type = _NumbaType()
_nb.jit = _jit
_nb.void = _nb.int32 = _nb.int64 = _nb.float32 = _nb.float64 = _nb_type
sys.modules.setdefault("numba", _nb)
```

**soxr mock** (`transformers ≥ 4.55` imports it at module top; never called on TTS path):
```python
_soxr = types.ModuleType("soxr")
_soxr.__version__ = "0.0.0"
_soxr.resample = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("soxr not available"))
for _q in ("VHQ", "HQ", "MQ", "LQ", "QQ"): setattr(_soxr, _q, _q)
sys.modules.setdefault("soxr", _soxr)
```

**boto3 mock** (`cached_path.schemes.r2` does `import boto3.session` at module top;
alternatively install real `boto3` with deps):
```python
for _m in ("boto3", "boto3.session", "boto3.dynamodb"):
    sys.modules.setdefault(_m, types.ModuleType(_m))
```

**MeCab (Japanese):** Patch `melo/text/japanese.py` wrapping the module-level import in `try/except`. See `models/melotts_zh/NOTES.md § Step 6`.

**python-mecab-ko (Korean):** Before `import melo`, inject a stub into `sys.modules`. The stub **must** expose `get_bert_feature` as an attribute — omitting it causes `ImportError: cannot import name 'get_bert_feature'`:
```python
_korean = types.ModuleType("melo.text.korean")
def _get_bert_feature(*a, **kw): raise RuntimeError("korean not supported on ARM64")
_korean.get_bert_feature = _get_bert_feature
sys.modules["melo.text.korean"] = _korean
```

**melo.download_utils replacement** (upstream S3 URLs return 403 Forbidden):
```python
import types, os
_dl = types.ModuleType("melo.download_utils")
def _load_or_download_config(locale):
    from melo import utils as _mu
    return _mu.get_hparams_from_file(os.environ["MELO_CONFIG_JSON"])
def _load_or_download_model(locale, device):
    return {"model": {}}   # dummy; real weights live in QNN .bin files
_dl.load_or_download_config = _load_or_download_config
_dl.load_or_download_model  = _load_or_download_model
sys.modules["melo.download_utils"] = _dl  # must be before `from melo.api import TTS`
```

**SynthesizerTrn.load_state_dict — relax strict=True** (dummy checkpoint has no weights):
```python
import melo.models as _mm
_orig = _mm.SynthesizerTrn.load_state_dict
def _lax(self, sd, strict=True, **kw): return _orig(self, sd, strict=False, **kw)
_mm.SynthesizerTrn.load_state_dict = _lax
```

> ℹ️ Install all TTS multi-`.bin` dependencies in one go (including `--no-deps` items and mocks); see `models/<id>/NOTES.md § Quick Start`. Do not trial-and-error package by package.

---

### Issue 7: Model I/O shape disagrees with metadata.json

`metadata.json` may differ from compiled `.bin` I/O order/shape. **Always confirm with `model.getInputName()` / `model.getInputShapes()`.**

---

### Issue 8: For NPU inference use only `qai_appbuilder` to load `.bin` / `.dlc`

All on-device (NPU/HTP) inference uses `qai_appbuilder.QNNContext`. The `.bin` filename varies by model; trust `metadata.json` and extracted files.

```python
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel, ProfilingLevel
QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.OFF)  # 2.47 signature: (runtime, log_level, profiling_level)
model = QNNContext("name", "encoder.bin")   # load the model's .bin, infer on the NPU
outputs = model.Inference([input_tensor])
```

> ℹ️ `.bin` is bound to its compile-time QAIRT version; mismatch reports `Error code: 5000`. The bundled QNN runtime is usually backward-compatible with older `.bin` files (e.g. 2.45-compiled loads on 2.46).
> ℹ️ Only plain ONNX uses `onnxruntime` `CPUExecutionProvider` for CPU baseline comparison; never run NPU models with it.

---

### Issue 9: `QNNContext` crash (0xC0000005) — `QNNConfig.Config()` not called

**Symptom:** Process exits with code `3221225477` (0xC0000005, ACCESS_VIOLATION), no Python traceback.
**Cause:** `appbuilder.pyd` global state (log level, profiling, HTP backend) uninitialized → null pointer dereference.

**Fix — first call in every process must be `QNNConfig.Config()`:**
```python
from qai_appbuilder import QNNContext, QNNConfig, LogLevel, Runtime, ProfilingLevel
QNNConfig.Config(Runtime.HTP, LogLevel.ERROR, ProfilingLevel.OFF)  # 2.47 signature: (runtime, log_level, profiling_level) — no lib-dir arg
model = QNNContext("name", "model.bin")             # only then can you load the model
```
Omitting `qnn_lib_path` auto-uses the package's `libs/` directory.

> ✅ A `.bin` compiled by older QAIRT (e.g. 2.45) loads on newer `qai_appbuilder` (e.g. 2.46).

---

Multiple `.bin`/`.dlc` contexts coexist fine in one process. For NPU-vs-CPU numerical comparison, run in **separate processes**:
```python
# Process A: .bin on NPU → outputs/qnn/*.npy
# Process B: .onnx + CPUExecutionProvider → outputs/cpu/*.npy
# Process C: load both, compute cosine
```

> ✅ Float model NPU vs CPU: typically cosine > 0.999; quantized models: accuracy ≥ 0.95.

---

### Issue 11: `Failed to create context with file mapping` warning — ignorable

```
[W] Failed to create context with file mapping enabled. ... Retrying with feature disabled.
```
ORT retries automatically and succeeds; no handling needed.

---

### Issue 12: `Inference()` input list must match `getInputName()` order

**Symptom:** `Inference()` hangs or produces garbled output.
**Cause:** Inputs matched by index, not name. Compiled graph order may differ from ONNX order.

```python
print(model.getInputName())    # confirm order
print(model.getInputShapes())  # confirm shapes
inputs = [t0, t1, ...]         # strictly in getInputName() order
outputs = model.Inference(inputs)

out_dict = dict(zip(model.getOutputName(), outputs))  # also look up outputs by name
result = out_dict["output_name"]
```

> ⚠️ Wrong order causes silent hang without exception. Multi-input models (e.g. streaming ASR cache tensors) are especially dangerous.

---

### Issue 13: Multi-`.bin` TTS/ASR package needs a custom pipeline

An AI Hub ZIP containing several `.bin` sub-models (encoder/flow/decoder/BERT etc.) can be loaded directly with `QNNContext`; no special SDK needed.
```
encoder.bin / flow.bin / decoder.bin / bert_wrapper.bin
config.json    ← sample rate, speaker ID, etc.
metadata.json  ← each sub-model's I/O shapes, quantization params
```
**Fix:** Call sub-models in pipeline order. Full example in `models/melotts_zh/NOTES.md`.

> ⚠️ **AI Hub `.bin` context binaries are loaded only with `qai_appbuilder.QNNContext`; never use `qnn-net-run` / `snpe-net-run`** — those CLIs expect standard QNN context binaries and will fail. Quantized sub-models need native dtype handling per Issue 5.

---

### Issue 14: ONNX ZIP with external weights — keep both files together

PyTorch 2.x `torch.onnx.export` splits large models into `<m>.onnx` (graph) + `<m>.data` (weights), linked by relative path.
**Extract the whole ZIP; keep both files in the same directory.** Copying `.onnx` alone fails.

---

### Issue 15: QNN DLC uses NHWC, ONNX uses NCHW

AI Hub QNN DLC image models: input `[1,H,W,C]`; ONNX: `[1,C,H,W]`.
```python
nhwc = preprocess(img)              # (1,H,W,C) → QNN DLC
nchw = nhwc.transpose(0, 3, 1, 2)  # (1,C,H,W) → ONNX
```
> Check `shape` in `metadata.json` to confirm layout before writing preprocessing.

---

- Sub-models (encoder/decoder/joiner) **must** come from the same AI Hub package (hidden dims differ across versions).
- **Debug order: encoder → decoder → joiner**: if recognition outputs only 1-2 tokens, check encoder output L2 norm (normal <20; ~50 = numerical explosion).
- `.bin` is QAIRT-version-sensitive: compare `tool_versions.qairt` in `metadata.json` with `_version` in `qairt_env.json`; mismatch → watch norm.

---

### Issue 17: Invalid test audio/input causes false "empty result"

Synthetic audio (sine wave/silence) produces empty ASR results, easily misjudged as a bug. **Verify input validity before inference:**
```python
energy = float(np.sqrt(np.mean(waveform.astype(np.float32)**2)))
if energy < 1e-3:
    print("[WARN] audio looks like silence/synthetic — empty result is expected")
```
Same for image tasks: use real images from `${APP_ROOT}\samples\images\`; Top-K on synthetic images is meaningless.

---

### Issue 18: 🚨 NEVER full-disk recursively scan to find a model package

**Symptom:** `Get-ChildItem -Recurse` runs 10+ min, agent times out.
**Rule:** locate packages via **fixed shallow paths only** (`C:\WoS_AI\<model>\`, `~\.qaihub\`) — NEVER recurse `C:\`, `C:\Users`, or unscoped `C:\WoS_AI`.
Normal flow: Step 1 (webfetch link) → curl to `C:/WoS_AI/<model>/`. If `qai_hub` installed, use it directly.
> ⚠️ Also applies to sub-agent task prompts: rewrite "search `C:\` for packages" into the fixed shallow-directory check above.

> 🚨 **MANDATORY — before inference execute Step 0.5 (read `NOTES.md`)** for known issues, I/O shapes, and ready-made inference scripts.

```
factory/chat_features/model-hub/models/
├── beit/          NOTES.md + infer_beit.py       (QNN_DLC w8a16, image classification)
├── melotts_zh/    NOTES.md + infer_melotts_zh.py (TTS, multi-`.bin`)
├── resnet50/      NOTES.md                       (QNN_DLC + ONNX float, comparison verified)
└── zipformer/     NOTES.md + infer_zipformer.py  (QNN_CONTEXT_BINARY, qai_appbuilder, streaming ASR)
```

**After completing a new model, create `NOTES.md`** recording: download links, chipset suffix, file structure, sub-model I/O table, performance data (latency, RTF), model-specific known issues (Issue Z-x format), inference command.

---

### Issue 19: SSL certificate verification failure on WoS (curl exit 35 / Python SSL error)

**Symptom:** `curl` exits 35 (`SSL connect error`) or Python `urllib` raises `ssl.SSLError` / `certificate verify failed` for `qaihub-public-assets.s3.us-west-2.amazonaws.com`.

**Cause:** WoS system CA bundle may lack the intermediate certificate for the S3 endpoint.

**Fix — always use `-k` with curl; disable cert verification in Python:**

```python
# curl: add -k to skip SSL certificate verification
exec('curl -k -L "<url>" -o "C:/WoS_AI/<model>/<file>.zip" --create-dirs', timeout=300)
```

```python
# Python urllib: create an unverified SSL context
import ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
urllib.request.urlopen(req, context=ctx, timeout=15)
```

> ⚠️ **Apply `-k` proactively** — don't wait for SSL errors. The S3 host is a known Qualcomm public asset server; skipping verification is safe.
> ⚠️ **S3 returns 403 for HEAD and Range-GET on public objects** — use plain GET to probe URL existence.

---

### Issue 20: `os._exit()` 触发进程异常退出（exit code `0xC0000409`）

在 `qai_appbuilder` 推理脚本中调用 `os._exit()` 会导致进程崩溃，exit code `0xC0000409`。让脚本正常跑完即可，Python 会有序析构所有 `QNNContext`，正常退出。

若确实需要提前退出，必须先 `del` 所有 `QNNContext` 对象，再调用 `os._exit()`：

```python
del model   # 先析构，触发 C++ 层释放
os._exit(0) # 此时再 exit 不会崩溃
```

> ⚠️ `DSP_INFO UNSUPPORTED_KEY: 49/50` 和 `Error 0x200: failed to close queue` 是非致命 HTP teardown 日志（见 Step 6），**不需要也不应该用 `os._exit()` 来规避它们**。
---
