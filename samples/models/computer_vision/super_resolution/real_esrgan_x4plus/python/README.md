# Real-ESRGAN x4plus

> Upscale any image 4×, running fully on the Snapdragon NPU (HTP).

![screenshot](../assets/screenshot.png)

## What it does
Real-ESRGAN x4plus takes an image and produces a **4× upscaled** version. It runs
the [Real-ESRGAN x4plus](https://aihub.qualcomm.com/compute/models/real_esrgan_x4plus)
super-resolution network (RRDBNet — 23 residual-in-residual dense blocks) **fully
on the Qualcomm NPU** through QAI AppBuilder's `QNNContext` / `OnnxRuntimeContext`
with HTP acceleration — no cloud, no internet needed at inference time (after the
initial one-time model download).

The pipeline resize/pads the input to the model's tile size, normalizes it,
auto-detects the tensor layout (NCHW vs NHWC) from the model's input shape, runs
inference in `PerfProfile.BURST`, then reassembles and un-pads the 4× result and
writes it to disk.

The script **auto-detects the platform** and picks a sensible runtime:

| Platform | Runtimes | Default |
|----------|----------|---------|
| Windows on Snapdragon (ARM64) | HTP / GPU / CPU | HTP |
| Windows x86_64 | CPU only, DLC only | CPU (forced) |
| ARM64 / x86_64 Linux | HTP / GPU / CPU | HTP |

## Models
| Model | Runtime | Precision | Source |
|-------|---------|-----------|--------|
| real_esrgan_x4plus (float DLC) | QNN / HTP | float | [Qualcomm AI Hub](https://aihub.qualcomm.com/compute/models/real_esrgan_x4plus) |

Four model formats are supported:
- **float DLC** (default) — auto-downloaded from the Qualcomm AI Hub public asset
  bucket on first run, extracted into `../models/`, and the archive deleted.
- **w8a8 quantized DLC** (`--w8a8`) — INT8, HTP only; auto-downloaded.
- **precompiled HTP binary** (`--bin`) — downloaded via QAI Hub for the detected SoC.
- **FP16 ONNX** (`--onnx`) — **auto-generated** on first use from the public
  `RealESRGAN_x4plus.pth` weights (downloaded, rebuilt as RRDBNet, exported to
  FP32 ONNX, converted to FP16), then loaded via `OnnxRuntimeContext`.

**No weights are committed to this repo.**

## Requirements
- OS: Windows on ARM64 (Snapdragon X Elite / X Plus), x86_64 Windows, or ARM64/x86_64 Linux
- Python >= 3.10
- qai_appbuilder >= 2.24.0
- `numpy`, `pillow`
- 16 GB RAM recommended

> **ONNX mode only:** `--onnx` additionally needs `onnxruntime` (plus
> `onnxruntime-qnn` for HTP), and — the first time it generates the ONNX file —
> `torch`, `onnx`, and `onnxconverter-common`. The default DLC/BIN path needs
> none of these. See the commented section in [`requirements.txt`](requirements.txt).

## Run
```bash
pip install -r requirements.txt
python real_esrgan_x4plus.py
```

On Windows you can also double-click [`start.bat`](start.bat), which creates a
local `.venv`, installs the dependencies, and launches the app.

From the `samples` directory you can also use the shared runner:
```bash
cd qai-appbuilder\samples

# Default (float DLC, auto-downloaded)
python run_inference.py --model real_esrgan_x4plus --args "--input_image_path path\to\image.jpg"

# Precompiled HTP binary / FP16 ONNX / w8a8 quantized DLC
python run_inference.py --model real_esrgan_x4plus --args "--bin"
python run_inference.py --model real_esrgan_x4plus --args "--onnx"
python run_inference.py --model real_esrgan_x4plus --args "--w8a8"
```

## Arguments
| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input_image_path` | `../assets/input.jpg` | Path to the input image |
| `--output_image_path` | `output.png` (next to the script) | Path to save the 4× upscaled output |
| `--bin` | False | Use precompiled HTP context binary |
| `--dlc` | True (default) | Use float DLC model |
| `--onnx` | False | Use FP16 ONNX via `OnnxRuntimeContext` (auto-generated) |
| `--w8a8` | False | Use w8a8 quantized DLC (implies `--dlc`, HTP only) |
| `--cpu` | False | Use CPU runtime (always active on x86 Windows) |
| `--gpu` | False | Use GPU runtime (not supported on x86 Windows) |
| `--no_show` | False | Don't open the image viewer after inference |
| `--chipset` | Auto-detected | SoC ID for hub-model download (Linux only) |

## Notes
- First run downloads the model (and, in `--onnx` mode, the PyTorch weights);
  subsequent runs reuse the local copies in `../models/`.
- `--bin` and `--dlc` are mutually exclusive; `--cpu` and `--gpu` are mutually
  exclusive. `.bin` and `--w8a8` require the HTP runtime and will error on CPU/GPU.
- `app.json` is the single source of truth for the gallery — see
  [../README.md](../README.md).
- Contribution standards: [../../docs/community.md](../../docs/community.md).

## Credits
Model: [Qualcomm AI Hub — Real-ESRGAN x4plus](https://aihub.qualcomm.com/compute/models/real_esrgan_x4plus).
Upstream weights: [xinntao/Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN).
