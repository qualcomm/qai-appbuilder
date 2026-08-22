# SKILL · Real-ESRGAN x4plus (Super-Resolution — App Builder Pack)

> Injected when user selects `real-esrgan-x4plus` Pack. Teaches how to *interpret*
> Real-ESRGAN output (upscaled image), not how to call it.
>
> Both flows valid: you MAY call `appbuilder_run` to verify I/O shape;
> user's WebUI calls it over HTTP API. A Run result may already be in
> conversation — interpret it using this file.

---

## 1. What this Pack does

Real-ESRGAN x4plus: image/video super-resolution from xinntao/Real-ESRGAN, running on Snapdragon X Elite NPU (QNN HTP FP16). Single QNN context binary:

1. **real_esrgan_480x640_fp16.bin** — RRDB-based generator, input [1,3,480,640] NCHW, output [1,3,1920,2560] NCHW

**Input:** RGB image of any size (PNG, JPG, JPEG, WebP, BMP, TIFF). Max 20 MB.

**Output:** JSON with `output_path`, `original_size`, `upscaled_size`, `scale`.

Fixed 4x upscale factor. Arbitrary input sizes handled via tiled inference with overlap blending (tile 640×480 input, 32px overlap, linear feathering at seams).

**Performance:** ~800 ms per tile on NPU. Total time depends on image size (number of tiles).

---

## 2. Parameters

| Param | Type | Default | Meaning |
|-------|------|---------|---------|
| `tile_size` | select: "480x640", "360x480" | `"480x640"` | Model input tile dimensions (HxW). Smaller tile uses less NPU memory but requires more tiles. |
| `tile_overlap` | number 0–64, step 8 | `32` | Pixel overlap between adjacent tiles (input side). Higher overlap reduces visible seams but increases processing time. |

Scale factor is always 4x — not configurable.

---

## 3. Output JSON Schema (canonical contract)

```jsonc
{
  "output_path": "data/outputs/<runId>_SRx4.png",  // repo-relative path; PNG or JPG
  "original_size": [640, 480],                      // [width, height] pixels
  "upscaled_size": [2560, 1920],                    // [width, height] = original × 4
  "scale": 4                                        // always 4
}
```

### 3.1 `output_path`

Repo-relative path to the upscaled image (`data/outputs/<runId>_SRx4.png`). Format matches input (PNG for PNG input, JPG quality 95 for JPG input).

### 3.2 `original_size`

`[width, height]` of the source image in pixels.

### 3.3 `upscaled_size`

`[width, height]` of the output image. Always `original_size × 4`.

### 3.4 `scale`

Integer upscale factor. Always 4. Echoed for consumer convenience.

---

## 4. Tiled inference details

### 4.1 Why tiles?

The model has a fixed input shape [1,3,480,640]. Images larger than 640×480 are split into overlapping tiles, each inferred independently, then blended back into a full-resolution output canvas.

### 4.2 Overlap blending

Adjacent tiles overlap by `tile_overlap` pixels (default 32). In the overlap region, a linear ramp (feathering) blends the two tiles to avoid visible seams. Output-side overlap = `tile_overlap × 4` = 128 pixels.

### 4.3 Edge handling

- If the image is smaller than one tile, it is padded with black to tile dimensions before inference; the output is cropped to `original_size × 4`.
- The last tile in each row/column is aligned to the image edge (may overlap more with its predecessor).

---

## 5. Typical user requests

### 5.1 "Upscale this photo"

Run already happened — upscaled image at `output_path`. User can view/download in UI.

### 5.2 "Can I upscale 8x?"

**Not directly.** This model is fixed at 4x. For 8x, run twice (output of first run as input to second). Quality degrades on second pass; manage expectations.

### 5.3 "Reduce file size of output"

Suggest saving as JPG with lower quality. The runner outputs PNG (lossless) by default for PNG inputs.

### 5.4 "Why are there seams/artifacts?"

Tile overlap too low. Suggest increasing `tile_overlap` to 48 or 64. Very rarely visible at default 32.

### 5.5 "Video super-resolution"

Supported via frame-by-frame processing. For video, the runner extracts frames, upscales each, and reassembles. Optical-flow interpolation is available in the full VideoSR WebUI but NOT in this isolated Pack runner.

---

## 6. Known limitations

- **Fixed 4x scale** — no 2x or 8x option in a single pass.
- **No denoising control** — Real-ESRGAN bakes denoising into the network; no separate strength knob.
- **No face enhancement** — unlike Real-ESRGAN with GFPGAN, this Pack does not include a face restoration stage.
- **No video temporal consistency** — each frame is processed independently; slight flicker may occur on video.
- **Large images are slow** — a 4K input requires many tiles; expect 10–30 seconds.
- **NPU memory** — if the 480×640 model fails to load (NPU memory exhausted), falls back to 360×480 tile or CPU bicubic.
- **Output format** — PNG for PNG/BMP/TIFF inputs; JPG (quality 95) for JPG/JPEG/WebP inputs.

---

## 7. Architecture

Single RRDB (Residual-in-Residual Dense Block) generator on Snapdragon X Elite NPU via QNN HTP FP16:

| Model | Input Shape | Output Shape | Role |
|-------|-------------|--------------|------|
| real_esrgan_480x640_fp16.bin | [1,3,480,640] | [1,3,1920,2560] | 4x upscale generator |

Tiled inference for arbitrary sizes; linear overlap blending; ~800 ms per tile on NPU.

---

## 8. What you (the LLM) must NOT do

- **Don't re-run to interpret existing results.** If Run result is in context, interpret it.
- **Don't modify** Pack files (developer-maintained). MAY `read` `runner.py` READ-ONLY for I/O understanding.
- **Don't invent fields** — only `output_path`, `original_size`, `upscaled_size`, `scale`.
- **Don't promise** 2x/8x/16x scale, face enhancement, denoising control, or temporal consistency.
- **Don't edit the output image.** Summarize quality, recommend re-run with different params — but no image editing.
- **Don't promise real-time video.** Per-frame processing; latency depends on resolution.

---

## 9. Quick reference — example output

Input: 640×480 photo, default params:

```jsonc
{
  "output_path":    "data/outputs/r-xyz789_SRx4.png",
  "original_size":  [640, 480],
  "upscaled_size":  [2560, 1920],
  "scale":          4
}
```

Single tile (image fits within 640×480), ~800 ms on NPU.


---

## 10. Model weight acquisition (user-converted via QAI ModelBuilder)

> **⚠️ This model does NOT have pre-compiled weights bundled or auto-downloaded.**
> Unlike melotts-zh / whisper-base / zipformer-zh (which ship pre-built QNN
> context binaries via Setup.bat), the Real-ESRGAN x4plus `.bin` must be
> converted by the user using QAI ModelBuilder's **model-builder** feature.

### 10.1 Why

The QNN context binary (`real_esrgan_480x640_fp16.bin`) is compiled for a
specific input shape (480×640) and target device. QAI ModelBuilder's
model-builder handles the full conversion pipeline (ONNX download → QNN
conversion → context binary generation) in-chat.

### 10.2 How to convert

In QAI ModelBuilder's chat interface, use the **model-builder** mode:

```
Convert Real-ESRGAN x4plus to QNN context binary, input size 480×640, fp16
```

The model-builder Agent will:
1. Download the Real-ESRGAN x4plus ONNX model
2. Convert to QNN format (FP16 precision)
3. Generate the `.bin` context binary for the local Snapdragon device
4. Place the output in the workspace

For the smaller fallback model (360×480 tile, lower NPU memory):
```
Convert Real-ESRGAN x4plus to QNN context binary, input size 360×480, fp16
```

### 10.3 Where to place the compiled .bin

After conversion, place (or symlink) the output context binary as:
```
models/real-esrgan-x4plus/real_esrgan_480x640_fp16.bin
models/real-esrgan-x4plus/real_esrgan_360x480_fp16.bin   (optional fallback)
```

The videosr-webui runner searches in this order:
1. `$APP_BUILDER_MODEL_ROOT/real-esrgan-x4plus/<filename>`
2. `<app_dir>/models/<filename>`

### 10.4 What to tell the user

If a user asks to run VideoSR and the model is not installed:
- Explain that this model must be converted first using QAI ModelBuilder's
  model-builder feature
- Suggest the prompt: "Convert Real-ESRGAN x4plus to QNN context binary,
  input size 480×640, fp16"
- Do NOT promise automatic download — it will not happen for this Pack
- After conversion, the user copies the `.bin` file to the model directory

---

## 11. User-facing setup guide (weights installation)

> Rendered as the `setup_guide` link when videosr-webui's model-missing pre-check
> reports `provisioning: "user-provided"`. This section targets the end user, not the LLM.

### 11.1 Purpose

videosr-webui runs 4× image/video super-resolution on the Snapdragon NPU using
**Real-ESRGAN x4plus** (RRDB generator from [xinntao/Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN))
compiled to a QNN context binary. No pre-compiled binary is bundled — you must
convert the reference PyTorch weights yourself, then drop the resulting `.bin`
into the model directory. This is a **one-time** setup.

### 11.2 Prerequisites

Pick ONE of the two conversion paths:

- **QAI Hub** (recommended, cloud-hosted) — sign up at <https://aihub.qualcomm.com/>
  and install the client: `pip install qai-hub qai-hub-models`.
- **AIMET** (AI Model Efficiency Toolkit, local) — see
  <https://github.com/quic/aimet> for install instructions per host OS.

Plus the reference PyTorch weights:

- `RealESRGAN_x4plus.pth` from the official release page:
  <https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.1.0>
  (direct: <https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth>).

### 11.3 Conversion steps

1. Download `RealESRGAN_x4plus.pth` from the link above.
2. Submit a **QAI Hub Compile Job** with:
   - Model: Real-ESRGAN x4plus (loaded from the `.pth`)
   - Input shape: `(1, 3, 480, 640)` NCHW, dtype `float32`
   - Target device: **Snapdragon X Elite** (or newer)
   - Runtime: **QNN Context Binary**
   - Precision: **fp16**
3. Download the resulting artifact — a single `.bin` context binary.

(AIMET path: quantize + export to QNN context binary with the same
input shape / device / precision; final artifact is likewise a single `.bin`.)

### 11.4 Installation

1. Rename the compiled artifact to `real_esrgan_480x640_fp16.bin`.
2. Create the model directory if it does not exist, then copy the file in:
   ```
   <repo>/models/real-esrgan-x4plus/real_esrgan_480x640_fp16.bin
   ```
   (`<repo>` is the QAIModelBuilder repo root — the same directory that contains
   `factory/` and `samples/`.)
3. Restart videosr-webui.

### 11.5 Verification

With videosr-webui running (default port `1975`):

- Open <http://127.0.0.1:1975/health> — expect `{"status":"ok", ...}`.
- Backend log on startup should include `[NPU] Using model: <...>/real_esrgan_480x640_fp16.bin`.
  If instead you see `[NPU] Model not found: real_esrgan_480x640_fp16.bin` the
  file is not on the expected path — recheck step 2 above.
- The model-status pre-check (P6) exposed on the app manager surfaces the
  missing file explicitly; once installed the entry drops off.

### 11.6 Alternative: lower-memory fallback variant

`backend/inference.py` also accepts an **optional** smaller tile variant used as
an automatic fallback when the 480×640 model fails to allocate on the NPU:

```
<repo>/models/real-esrgan-x4plus/real_esrgan_360x480_fp16.bin
```

Convert it the same way but with input shape `(1, 3, 360, 480)`. Purely
optional — the app runs fine with just the primary 480×640 binary.

**Accepted filenames** (from `backend/inference.py`, verified 2026-07-25):

| Filename                            | Role                | Required |
|-------------------------------------|---------------------|----------|
| `real_esrgan_480x640_fp16.bin`      | Primary NPU model   | Yes      |
| `real_esrgan_360x480_fp16.bin`      | Low-memory fallback | No       |

No other filenames are recognized — do not rename to `192x256` / etc.