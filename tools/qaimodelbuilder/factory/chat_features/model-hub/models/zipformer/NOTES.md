# Zipformer — Model Notes

## Overview

| Item | Value |
|------|-------|
| AI Hub ID | `zipformer` |
| HuggingFace | https://huggingface.co/qualcomm/Zipformer |
| Task | Streaming ASR — mixed Chinese/English (Mandarin + English) |
| Format | **`QNN_CONTEXT_BINARY` (`.bin`) → run inference on the NPU with `qai_appbuilder` / `QNNContext`** |
| Inference tool | `qai_appbuilder.QNNContext` (loads `encoder.bin` / `decoder.bin` / `joiner.bin`) |
| Inference script | `infer_zipformer.py` (this directory) |
| Local path | `C:\WoS_AI\zipformer\` |

> 🚨 **NPU inference always uses `qai_appbuilder` to load the `.bin`** (consistent with other models like beit / melotts_zh / resnet50).
> Downloading the `QNN_CONTEXT_BINARY` format package gives you the three cleanly-named `.bin` files, with no extra runtime needed.

---

## Download (QNN_CONTEXT_BINARY, v0.55.0)

First detect this machine's chipset per SKILL.md Step 0, then download the zip **matching this machine**:

**Snapdragon X Elite (`qcadsp*_8380`):**
```
https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/zipformer/releases/v0.55.0/zipformer-qnn_context_binary-float-qualcomm_snapdragon_x_elite.zip
```

**Snapdragon X2 Elite (`qcadsp*_8480`):**
```
https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/zipformer/releases/v0.55.0/zipformer-qnn_context_binary-float-qualcomm_snapdragon_x2_elite.zip
```

Download + extract (use Python `zipfile`, **not** `tar`):
```python
import zipfile
zipfile.ZipFile(r"C:\WoS_AI\zipformer\zipformer-qnn_context_binary-float-qualcomm_snapdragon_x_elite.zip").extractall(r"C:\WoS_AI\zipformer")
```

> ℹ️ The repo's `models/zipformer-zh/` already has a usable set of `encoder.bin` / `decoder.bin` / `joiner.bin` +
> `metadata.json` (X Elite, QAIRT 2.45). The `--model_dir` of `infer_zipformer.py` points to it by default,
> so on an X Elite machine you can run it directly without re-downloading; on X2 Elite, download the corresponding X2 zip above and point `--model_dir` at it.

---

## Files in ZIP

```
zipformer-qnn_context_binary-float-qualcomm_snapdragon_x_elite/
├── encoder.bin      ← chunked streaming Zipformer encoder (35 cache tensors I/O)
├── decoder.bin      ← RNN-T prediction network (2-token context)
├── joiner.bin       ← encoder × decoder → vocab logits
└── metadata.json    ← each sub-model's I/O shapes / dtypes + QAIRT version
```

All three `.bin` files are QNN context binaries, loaded with `qai_appbuilder.QNNContext` (`runtime="Htp"`,
`input_data_type="native"` + `output_data_type="native"`, because the cache contains a mix of int32 + float32 dtypes).

---

## Pipeline

```
audio (16 kHz mono WAV)
  → kaldi_native_fbank log-mel features [T, 80]
  → for each chunk (OFFSET=64-frame step, SEGMENT=71-frame window, zero-pad if short):
      encoder.Inference([chunk] + 35 cache tensors) → 36 outputs
        = 35 new cache (fed to the next chunk) + encoder_out [1,16,512] (at the end, index 35)
      for t in 0..16:
        joiner.Inference([encoder_out[t] (1,512), decoder_out (1,512)]) → logit [1,6254]
          → argmax → if not blank(id 0): append token, refresh decoder_out with the latest 2 tokens
  → token IDs → text (assets/tokens.txt, sherpa-onnx `<token> <id>` format)
```

Geometry constants (must match the QNN export): `DECODE_CHUNK_SIZE=32`, `OFFSET=64`, `SEGMENT=71`,
`ENCODER_FRAMES_PER_CHUNK=16`, `BLANK_ID=0`, `CONTEXT_SIZE=2`.

---

## Sub-models I/O (from metadata.json)

### encoder.bin — 36 inputs / 36 outputs

| | Tensor | Shape | Dtype |
|-|--------|-------|-------|
| In | `x` | `[1, 71, 80]` | float32 — fbank features of one chunk |
| In | `cached_len_{0..4}` | `[n, 1]` | int32 |
| In | `cached_avg_{0..4}` | `[n, 1, 384]` | float32 |
| In | `cached_key_{0..4}` | `[n, kh, 1, 192]` | float32 |
| In | `cached_val_{0..4}` | `[n, vh, 1, 96]` | float32 |
| In | `cached_val2_{0..4}` | `[n, vh, 1, 96]` | float32 |
| In | `cached_conv1_{0..4}` | `[n, 1, 384, 30]` | float32 |
| In | `cached_conv2_{0..4}` | `[n, 1, 384, 30]` | float32 |
| Out | `new_cached_*` | same as the corresponding input | — fed to the next chunk |
| Out | `encoder_out` | `[1, 16, 512]` | float32 — **at the end of the output list (index 35)** |

Per-layer `(n, kh, vh)` config (5 layers total, 7 cache tensors per layer = 35):

| layer | n | kh | vh |
|-------|---|----|----|
| 0 | 2 | 128 | 128 |
| 1 | 4 | 64 | 64 |
| 2 | 3 | 32 | 32 |
| 3 | 2 | 16 | 16 |
| 4 | 4 | 64 | 64 |

### decoder.bin

| | Tensor | Shape | Dtype |
|-|--------|-------|-------|
| In | `y` | `[1, 2]` | int32 — the latest 2 token ids |
| Out | `decoder_out` | `[1, 512]` | float32 |

### joiner.bin

| | Tensor | Shape | Dtype |
|-|--------|-------|-------|
| In | `encoder_out` | `[1, 512]` | float32 |
| In | `decoder_out` | `[1, 512]` | float32 |
| Out | `logit` | `[1, 6254]` | float32 |

Token 0 = `<blk>` (blank). Greedy decoding: argmax, skip blank, on non-blank append and refresh the decoder.

---

## Feature Extraction（kaldi_native_fbank log-mel）

| Param | Value |
|-------|-------|
| Sample rate | 16 000 Hz (mono) |
| Mel bins | 80 |
| high_freq | -400 (Nyquist − 400 Hz) |
| dither | 0 |
| snip_edges | False |

```python
import kaldi_native_fbank as knf
opts = knf.FbankOptions()
opts.frame_opts.dither = 0
opts.frame_opts.snip_edges = False
opts.frame_opts.samp_freq = 16000
opts.mel_opts.num_bins = 80
opts.mel_opts.high_freq = -400.0
```

> Settings match the sherpa-onnx mixed Chinese/English zipformer training recipe. Install: `uv pip install kaldi_native_fbank`
> (the ARM64 wheel must match the Python version of `python_arm64_venv`).

---

## Vocabulary

- 6254-dim logit (matching the joiner output); `assets/tokens.txt` is in sherpa-onnx `<token> <id>` format.
- The repo already has `factory/app_builder/models/zipformer-zh/assets/tokens.txt` for reuse;
  or obtain the `tokens.txt` of the same checkpoint from the sherpa-onnx upstream.

---

## Test Audio

Use a real 16 kHz mono Chinese-speech WAV (synthetic/silent audio makes the result empty, see Issue Z-4).
The official sherpa-onnx test audio from the same source as this model can be used directly:

- **Download source**: `https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23/resolve/main/test_wavs/0.wav`
- **Already downloaded to**: `C:\WoS_AI\zipformer\test_zh.wav` (16 kHz mono, ~5.6 s of Chinese speech)

```powershell
Invoke-WebRequest `
  -Uri "https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23/resolve/main/test_wavs/0.wav" `
  -OutFile "C:\WoS_AI\zipformer\test_zh.wav"
```

> Verified: on Snapdragon X2 Elite this audio recognizes as
> 「对我做了介绍啊那么我想说的是呢大家如果对我的研究感兴趣」, RTF ≈ 0.02.

---

## Inference Command

```powershell
# By default uses the .bin under the repo's preset models/zipformer-zh/ (X Elite)
& "<python_arm64_venv>\Scripts\python.exe" `
  "${APP_ROOT}\skills\aihub-model-run\models\zipformer\infer_zipformer.py" `
  --audio "C:\path\to\audio_16khz_mono.wav"

# Or specify a model directory you downloaded/extracted yourself (e.g. the X2 Elite package)
& "<python_arm64_venv>\Scripts\python.exe" `
  "...\infer_zipformer.py" `
  --audio "C:\path\to\audio.wav" `
  --model_dir "C:\WoS_AI\zipformer\zipformer-qnn_context_binary-float-qualcomm_snapdragon_x2_elite" `
  --tokens "${APP_ROOT}\factory\app_builder\models\zipformer-zh\assets\tokens.txt"
```

`<python_arm64_venv>` is read from `${APP_ROOT}\data\config\qairt_env.json`, do not hardcode it.

---

## Model-Specific Issues

### Issue Z-1: encoder input order — cache is grouped **by layer**, not by type

The input list order of `encoder.Inference(inputs)` is fixed: first `x`, then **layer by layer** give that layer's 7 cache tensors
(`len/avg/key/val/val2/conv1/conv2`), 5 layers total = 35 cache. A wrong order causes `Inference()`
to silently hang or produce garbled output (see SKILL.md Issue 12).

```python
inputs = [x_chunk]
for li, (n, kh, vh) in enumerate(LAYER_CONFIGS):
    inputs += [state[li]["len"], state[li]["avg"], state[li]["key"],
               state[li]["val"], state[li]["val2"], state[li]["conv1"], state[li]["conv2"]]
outputs = encoder.Inference(inputs)
```

> Use `qnn_enc.getInputName()` to verify the actual order (it may change after an SDK version update).

### Issue Z-2: encoder output — `encoder_out` is the **last one** (index 35), not the first

`getOutputName()` returns `[new_cached_*..., encoder_out]`, with `encoder_out` (shape `(1,16,512)`) at the end.
When splitting outputs: the first 35 are the new cache (fed to the next chunk), and only the last 1 is `encoder_out`.

### Issue Z-3: cache uses NATIVE dtype, mixing int32 / float32

`cached_len_*` are int32, while the rest of the cache and `x` are float32. All three contexts are loaded with
`input_data_type="native"` + `output_data_type="native"`, passing in each tensor by its original dtype,
do not uniformly convert to float.

### Issue Z-4: use real speech for the test audio

Synthetic audio (sine wave/silence) makes the ASR result empty (see SKILL.md Issue 17). Verify the audio energy before inference,
and use a real 16 kHz mono speech WAV.

---

## Reference Implementation

The repo's `factory/app_builder/models/zipformer-zh/runner.py` is the complete `qai_appbuilder` inference implementation
of the same model under the App Builder module (streaming encoder + greedy RNN-T decoding + token de-mapping);
this directory's `infer_zipformer.py` was ported from its algorithm into a standalone script. The two share the same algorithm/geometry constants.
