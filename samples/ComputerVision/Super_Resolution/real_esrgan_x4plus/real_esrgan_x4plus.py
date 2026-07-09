# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
#
# Unified script for real_esrgan_x4plus inference.
# Supports five platforms (auto-detected at runtime):
#   - wos        Windows on Snapdragon (ARM64) — HTP / GPU / CPU
#   - x86_win    Windows x86_64               — CPU only, DLC only
#   - arm64_linux  ARM64 Linux                — HTP / GPU / CPU
#   - x86_linux    x86_64 Linux               — HTP / GPU / CPU
#   - unknown    (falls back to CPU)
#
# Default runtime : HTP  (x86_win is always forced to CPU)
# Default model   : .dlc (float DLC, auto-downloaded)
#                   Use --bin for precompiled HTP context binary.
#
# CLI options:
#   --bin              Use .bin precompiled HTP context binary instead of .dlc
#   --dlc              Explicitly use float .dlc model (already the default)
#   --onnx             Use models/real_esrgan_x4plus.onnx via OnnxRuntimeContext
#                      (onnxruntime_qnn HTP EP); auto-generates the FP16 ONNX
#                      file if it is absent.
#   --cpu              Use CPU runtime instead of HTP
#   --gpu              Use GPU runtime instead of HTP
#   --w8a8             Use w8a8 quantised DLC instead of float DLC
#                      (only meaningful with --dlc)
#   --chipset <id>     Override SoC ID used for hub-model download
#   --input_image_path <path>   Input image  (default: <script_dir>/input.jpg)
#   --output_image_path <path>  Output image (default: <script_dir>/output.png)
#   --no_show          Do not pop up image viewer after inference
# ---------------------------------------------------------------------

import sys
import os
import platform
import argparse
import ssl
import urllib.request
import zipfile
import shutil
import time
import warnings

sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "common"))

import install
import numpy as np
from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from image_processing import (
    pil_resize_pad,
    pil_undo_resize_pad,
)
from qai_appbuilder import (
    QNNContext,
    OnnxRuntimeContext,
    Runtime,
    LogLevel,
    ProfilingLevel,
    PerfProfile,
    QNNConfig,
)
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Model metadata
# ─────────────────────────────────────────────────────────────────────────────
MODEL_ID_WOS   = "mnz1l2exq"   # WoS / x86-win hub model ID
MODEL_ID_LINUX = "mqkrre6wn"   # Linux hub model ID
MODEL_NAME     = "real_esrgan_x4plus"
MODEL_HELP_URL = (
    "https://github.com/qualcomm/qai-appbuilder/tree/main/samples/python/"
    + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
)

# Public DLC download URLs  (v0.55.0)
MODEL_DLC_FLOAT_URL = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
    "qai-hub-models/models/real_esrgan_x4plus/releases/v0.55.0/"
    "real_esrgan_x4plus-qnn_dlc-float.zip"
)
MODEL_DLC_W8A8_URL = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
    "qai-hub-models/models/real_esrgan_x4plus/releases/v0.55.0/"
    "real_esrgan_x4plus-qnn_dlc-w8a8.zip"
)

# PyTorch weights URL for FP16 ONNX generation
_PTH_URL = (
    "https://github.com/xinntao/Real-ESRGAN"
    "/releases/download/v0.1.0/RealESRGAN_x4plus.pth"
)

# DLC file name suffixes stored on disk
DLC_FLOAT_SUFFIX = ".dlc"           # models/real_esrgan_x4plus.dlc
DLC_W8A8_SUFFIX  = "-w8a8.dlc"     # models/real_esrgan_x4plus-w8a8.dlc

# Fallback image size; overridden at runtime from model input shape when possible.
IMAGE_SIZE = 128

# Default IMAGE_SIZE used when the model reports dynamic (-1) spatial dimensions.
IMAGE_SIZE_DEFAULT = 512

# ─────────────────────────────────────────────────────────────────────────────
# Platform / device detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_platform():
    """Return one of: 'wos', 'x86_win', 'arm64_linux', 'x86_linux', 'unknown'."""
    system  = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        if machine in ("aarch64", "arm64"):
            return "wos"          # Windows on Snapdragon
        else:
            return "x86_win"      # Regular x86_64 Windows
    if system == "linux":
        if machine in ("aarch64", "arm64"):
            return "arm64_linux"
        if machine in ("x86_64", "amd64"):
            return "x86_linux"
    return "unknown"

PLATFORM = _detect_platform()
print(f"[INFO] Detected platform: {PLATFORM}")

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

execution_ws = Path(os.path.dirname(os.path.abspath(__file__)))


model_dir = execution_ws / "models"

# ─────────────────────────────────────────────────────────────────────────────
# Global state
# ─────────────────────────────────────────────────────────────────────────────
image_buffer = None
realesrgan   = None

# ─────────────────────────────────────────────────────────────────────────────
# Model class
# ─────────────────────────────────────────────────────────────────────────────

class RealESRGan(QNNContext):
    def Inference(self, input_data):
        input_datas = [input_data]
        # Pass PerfProfile.BURST directly so the HTP runs at maximum clock speed.
        # Using DEFAULT here would silently fall back to a lower power state and
        # roughly double the inference latency on float32 .bin models.
        output_data = super().Inference(input_datas, perf_profile=PerfProfile.BURST)[0]
        return output_data

# ─────────────────────────────────────────────────────────────────────────────
# Layout helpers
# ─────────────────────────────────────────────────────────────────────────────

def _guess_layout_from_shape(shape4):
    """Infer NCHW / NHWC from a 4-D input shape."""
    if len(shape4) != 4:
        return None
    c_candidates = {1, 3, 4}
    if shape4[1] in c_candidates and shape4[-1] not in c_candidates:
        return "NCHW"
    if shape4[-1] in c_candidates and shape4[1] not in c_candidates:
        return "NHWC"
    if shape4[1] in c_candidates:
        return "NCHW"
    return "NHWC"


def _set_image_size_from_model():
    """Update IMAGE_SIZE using the model's expected input shape.

    ONNX models with dynamic spatial axes report -1 for H and W.  In that
    case we fall back to IMAGE_SIZE_DEFAULT (512) which is the tile size
    this model was trained on.
    """
    global IMAGE_SIZE
    try:
        shapes = realesrgan.getInputShapes()
        if shapes and len(shapes[0]) == 4:
            layout = _guess_layout_from_shape(shapes[0])
            detected = int(shapes[0][2]) if layout == "NCHW" else int(shapes[0][1])
            print(f"[INFO] Detected input layout : {layout}")
            if detected > 0:
                IMAGE_SIZE = detected
            else:
                IMAGE_SIZE = IMAGE_SIZE_DEFAULT
                print(f"[INFO] Dynamic spatial dims detected (-1); "
                      f"using default IMAGE_SIZE: {IMAGE_SIZE}")
            print(f"[INFO] Using IMAGE_SIZE       : {IMAGE_SIZE}")
    except Exception as e:
        print(f"[WARN] Failed to infer IMAGE_SIZE from model input shape: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Model download helpers
# ─────────────────────────────────────────────────────────────────────────────

def _download_dlc(dlc_path: Path, url: str, zip_filename: str):
    """Download and extract a DLC zip to dlc_path if it does not already exist.

    Parameters
    ----------
    dlc_path     : destination path for the extracted .dlc file
    url          : HTTPS URL of the zip archive
    zip_filename : local filename to use while downloading the zip
    """
    if dlc_path.is_file():
        print(f"[INFO] DLC model already exists: {dlc_path}")
        return

    zip_path = execution_ws / zip_filename

    print(f"[INFO] Downloading DLC model from:\n  {url}")
    try:
        urllib.request.urlretrieve(url, str(zip_path))
        print(f"[INFO] Download complete: {zip_path}")
    except Exception as e:
        print(f"[ERROR] Failed to download DLC model: {e}")
        sys.exit(1)

    extract_dir = execution_ws / "_dlc_extract_tmp"
    print(f"[INFO] Extracting {zip_filename} …")
    try:
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            zf.extractall(str(extract_dir))
    except Exception as e:
        print(f"[ERROR] Failed to extract zip: {e}")
        zip_path.unlink(missing_ok=True)
        sys.exit(1)

    # Locate any .dlc file inside the extracted tree
    found_dlc = None
    for root, _dirs, files in os.walk(str(extract_dir)):
        for fname in files:
            if fname.endswith(".dlc"):
                found_dlc = Path(root) / fname
                break
        if found_dlc:
            break

    if not found_dlc:
        print(f"[ERROR] No .dlc file found in the extracted zip.")
        shutil.rmtree(str(extract_dir), ignore_errors=True)
        zip_path.unlink(missing_ok=True)
        sys.exit(1)

    model_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(found_dlc), str(dlc_path))
    print(f"[INFO] Copied DLC to: {dlc_path}")

    # Cleanup
    shutil.rmtree(str(extract_dir), ignore_errors=True)
    zip_path.unlink(missing_ok=True)
    print(f"[INFO] Removed temporary zip: {zip_path}")


def _download_dlc_float(dlc_path: Path):
    """Download the float DLC."""
    _download_dlc(
        dlc_path,
        url=MODEL_DLC_FLOAT_URL,
        zip_filename="real_esrgan_x4plus-qnn_dlc-float.zip",
    )


def _download_dlc_w8a8(dlc_path: Path):
    """Download the w8a8 quantised DLC."""
    _download_dlc(
        dlc_path,
        url=MODEL_DLC_W8A8_URL,
        zip_filename="real_esrgan_x4plus-qnn_dlc-w8a8.zip",
    )


def _download_bin(bin_path: Path, soc_id, model_id):
    """Download .bin model via QAI Hub if bin_path does not exist."""
    if bin_path.is_file():
        print(f"[INFO] BIN model already exists: {bin_path}")
        return

    desc = f"Downloading {MODEL_NAME} model… "
    fail = (
        f"\nFailed to download {MODEL_NAME} model. "
        f"Please prepare the model according to:\n{MODEL_HELP_URL}"
    )

    ret = install.download_qai_hubmodel(soc_id, MODEL_NAME, str(bin_path), desc=desc, fail=fail)

    if not ret:
        sys.exit(1)

# -----------------------------------------------------------------------------
# FP16 ONNX auto-generation helpers
# (adapted from get_real_esrgan_x4plus_fp16.py)
# -----------------------------------------------------------------------------

def _check_onnx_deps() -> bool:
    """Return True if all dependencies for ONNX generation are available."""
    missing = []
    for pkg, pip in [("torch", "torch"), ("onnx", "onnx"),
                     ("onnxconverter_common", "onnxconverter-common")]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pip)
    if missing:
        print(f"[ERROR] Missing dependencies for ONNX generation: {', '.join(missing)}")
        print(f"        Please install them:  pip install {' '.join(missing)}")
        return False
    return True


def _build_rrdbnet():
    """Return a Real-ESRGAN x4plus RRDBNet instance (weights not loaded)."""
    import torch.nn as nn
    import torch.nn.functional as F
    from torch.nn import init

    def _init_weights(modules, scale=0.1):
        for m in (modules if isinstance(modules, list) else [modules]):
            for layer in m.modules():
                if isinstance(layer, nn.Conv2d):
                    init.kaiming_normal_(layer.weight)
                    layer.weight.data *= scale
                    if layer.bias is not None:
                        layer.bias.data.zero_()

    class ResidualDenseBlock(nn.Module):
        def __init__(self, num_feat=64, num_grow_ch=32):
            super().__init__()
            self.conv1 = nn.Conv2d(num_feat,                 num_grow_ch, 3, 1, 1)
            self.conv2 = nn.Conv2d(num_feat +   num_grow_ch, num_grow_ch, 3, 1, 1)
            self.conv3 = nn.Conv2d(num_feat + 2*num_grow_ch, num_grow_ch, 3, 1, 1)
            self.conv4 = nn.Conv2d(num_feat + 3*num_grow_ch, num_grow_ch, 3, 1, 1)
            self.conv5 = nn.Conv2d(num_feat + 4*num_grow_ch, num_feat,    3, 1, 1)
            self.lrelu = nn.LeakyReLU(0.2, inplace=True)
            _init_weights([self.conv1, self.conv2, self.conv3,
                           self.conv4, self.conv5])

        def forward(self, x):
            import torch
            x1 = self.lrelu(self.conv1(x))
            x2 = self.lrelu(self.conv2(torch.cat([x, x1], 1)))
            x3 = self.lrelu(self.conv3(torch.cat([x, x1, x2], 1)))
            x4 = self.lrelu(self.conv4(torch.cat([x, x1, x2, x3], 1)))
            x5 = self.conv5(torch.cat([x, x1, x2, x3, x4], 1))
            return x5 * 0.2 + x

    class RRDB(nn.Module):
        def __init__(self, num_feat, num_grow_ch=32):
            super().__init__()
            self.rdb1 = ResidualDenseBlock(num_feat, num_grow_ch)
            self.rdb2 = ResidualDenseBlock(num_feat, num_grow_ch)
            self.rdb3 = ResidualDenseBlock(num_feat, num_grow_ch)

        def forward(self, x):
            out = self.rdb3(self.rdb2(self.rdb1(x)))
            return out * 0.2 + x

    class RRDBNet(nn.Module):
        """Real-ESRGAN x4plus backbone: 3→3 channels, 4× upscale, 23 RRDB blocks."""
        def __init__(self):
            super().__init__()
            nf, nb, gc = 64, 23, 32
            self.conv_first = nn.Conv2d(3,  nf, 3, 1, 1)
            self.body       = nn.Sequential(*[RRDB(nf, gc) for _ in range(nb)])
            self.conv_body  = nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_up1   = nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_up2   = nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_hr    = nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_last  = nn.Conv2d(nf, 3,  3, 1, 1)
            self.act        = nn.LeakyReLU(0.2, inplace=True)

        def forward(self, x):
            feat      = self.conv_first(x)
            body_feat = self.conv_body(self.body(feat))
            feat      = feat + body_feat
            feat = self.act(self.conv_up1(F.interpolate(feat, scale_factor=2, mode="nearest")))
            feat = self.act(self.conv_up2(F.interpolate(feat, scale_factor=2, mode="nearest")))
            return self.conv_last(self.act(self.conv_hr(feat)))

    return RRDBNet()


def _progress_hook(block_num, block_size, total_size):
    downloaded = block_num * block_size
    if total_size > 0:
        pct = min(downloaded / total_size * 100, 100)
        bar = "#" * int(pct / 2)
        print(f"\r  [{bar:<50}] {pct:5.1f}%  "
              f"({downloaded/1e6:.1f}/{total_size/1e6:.1f} MB)", end="", flush=True)


def _download_pth(pth_path: Path) -> None:
    """Download RealESRGAN_x4plus.pth weights if not already present."""
    if pth_path.is_file():
        size_mb = pth_path.stat().st_size / 1e6
        print(f"[INFO] PyTorch weights already exist: {pth_path}  ({size_mb:.1f} MB)")
        return
    print(f"[INFO] Downloading RealESRGAN_x4plus.pth (~64 MB) ...")
    print(f"       Source: {_PTH_URL}")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    urllib.request.urlretrieve(_PTH_URL, str(pth_path), reporthook=_progress_hook)
    print()
    print(f"[OK]  Saved: {pth_path}  ({pth_path.stat().st_size/1e6:.1f} MB)")


def _export_fp32_onnx(model, fp32_path: Path) -> None:
    import torch
    # IMPORTANT: Export with a STATIC input shape (no dynamic_axes).
    #
    # The QNN HTP (NPU) execution provider requires fully static input
    # dimensions. If the ONNX graph declares dynamic H/W axes, QNN resolves
    # them to 0 at setup time and fails with "Zero tensor size!", then silently
    # falls back to the CPU execution provider.
    #
    # We therefore bake a fixed [1, 3, IMAGE_SIZE, IMAGE_SIZE] input so the
    # model runs on HTP. IMAGE_SIZE (128) matches the tile size used by the
    # inference pre-processing (pil_resize_pad to IMAGE_SIZE x IMAGE_SIZE).
    dummy = torch.zeros(1, 3, IMAGE_SIZE, IMAGE_SIZE, dtype=torch.float32)
    print(f"[INFO] Exporting FP32 ONNX (static {IMAGE_SIZE}x{IMAGE_SIZE} input) → {fp32_path}")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with torch.no_grad():
            torch.onnx.export(
                model,
                dummy,
                str(fp32_path),
                opset_version=18,
                input_names=["input"],
                output_names=["output"],
                # No dynamic_axes -> fully static shapes, required by QNN HTP.
                do_constant_folding=True,
                dynamo=False,
            )
    print(f"[OK]  FP32 ONNX saved: {fp32_path}  ({fp32_path.stat().st_size/1e6:.1f} MB)")


def _convert_to_fp16(fp32_path: Path, fp16_path: Path) -> None:
    import onnx
    from onnxconverter_common import convert_float_to_float16
    print(f"[INFO] Converting FP32 → FP16 ONNX ...")
    model_fp32 = onnx.load(str(fp32_path))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model_fp16 = convert_float_to_float16(model_fp32, keep_io_types=False)
    onnx.save(model_fp16, str(fp16_path))
    print(f"[OK]  FP16 ONNX saved: {fp16_path}  ({fp16_path.stat().st_size/1e6:.1f} MB)")


def ensure_onnx_model(onnx_path: Path) -> bool:
    """Ensure the FP16 ONNX model exists at onnx_path.

    If the file is missing, this function:
      1. Downloads RealESRGAN_x4plus.pth weights.
      2. Builds the RRDBNet architecture and loads the weights.
      3. Exports a FP32 ONNX (intermediate).
      4. Converts to FP16 ONNX and saves to onnx_path.

    Returns True on success, False on failure.
    """
    if onnx_path.is_file():
        print(f"[INFO] ONNX model already exists: {onnx_path}")
        return True

    print(f"[INFO] ONNX model not found: {onnx_path}")
    print(f"[INFO] Auto-generating FP16 ONNX model ...")

    if not _check_onnx_deps():
        return False

    import torch

    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir   = execution_ws / "_onnx_build_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    pth_path  = tmp_dir / "RealESRGAN_x4plus.pth"
    fp32_path = tmp_dir / "real_esrgan_x4plus_fp32.onnx"
    fp16_path = tmp_dir / "real_esrgan_x4plus_fp16.onnx"

    try:
        # Step 1: download weights
        print("[ Step 1/4 ] Downloading PyTorch weights")
        _download_pth(pth_path)

        # Step 2: load model
        print("[ Step 2/4 ] Loading RRDBNet model")
        model = _build_rrdbnet()
        state = torch.load(str(pth_path), map_location="cpu", weights_only=True)
        state = state.get("params_ema", state.get("params", state))
        model.load_state_dict(state, strict=True)
        model.eval()
        param_m = sum(p.numel() for p in model.parameters()) / 1e6
        print(f"[OK]  Model loaded, parameters: {param_m:.1f} M")

        # Step 3: export FP32 ONNX
        print("[ Step 3/4 ] Exporting FP32 ONNX (intermediate)")
        _export_fp32_onnx(model, fp32_path)

        # Step 4: convert to FP16
        print("[ Step 4/4 ] Converting to FP16 ONNX")
        _convert_to_fp16(fp32_path, fp16_path)

        # Copy to final destination
        shutil.copy2(str(fp16_path), str(onnx_path))
        print(f"[OK]  FP16 ONNX copied to: {onnx_path}  "
              f"({onnx_path.stat().st_size/1e6:.1f} MB)")
        return True

    except Exception as e:
        print(f"[ERROR] Failed to generate ONNX model: {e}")
        return False
    finally:
        # Clean up temp directory
        shutil.rmtree(str(tmp_dir), ignore_errors=True)

# -----------------------------------------------------------------------------
# ONNX / OnnxRuntimeContext init path (WoS HTP via onnxruntime_qnn)
# -----------------------------------------------------------------------------

def _init_onnx_htp(onnx_path: Path, use_cpu: bool = False):
    """Initialise inference using OnnxRuntimeContext (onnxruntime_qnn HTP EP).

    If the ONNX model file does not exist, it is automatically generated as a
    FP16 ONNX by downloading the PyTorch weights and converting them.

    OnnxRuntimeContext (from qai_appbuilder.qnncontext) automatically:
      - Imports onnxruntime_qnn and registers the QNN Execution Provider.
      - Selects the HTP (NPU) backend via QAI_ORTQNN_BACKEND=htp (default).
      - Enables FP16 precision on HTP (enable_htp_fp16_precision=1).
      - Falls back to EPContext precompile workaround on layout-transform errors.
      - Falls back to CPU if QNN EP is unavailable.

    Environment variables that influence behaviour (all optional):
      QAI_ORT_ONNX_USE_ORTQNN   : "1" (default) to use onnxruntime_qnn
      QAI_ORTQNN_BACKEND         : "htp" (default) | "cpu" | "gpu"
      QAI_ORTQNN_ENABLE_HTP_FP16 : "1" (default) to enable FP16 precision
      QAI_ORTQNN_TRY_CONTEXT_CACHE : "1" (default) to retry with EPContext
    """
    global realesrgan

    # Auto-generate the FP16 ONNX if it is missing
    if not onnx_path.is_file():
        ok = ensure_onnx_model(onnx_path)
        if not ok:
            print(f"[ERROR] Could not obtain ONNX model at: {onnx_path}")
            print(f"        Please place the model manually or install the required "
                  f"dependencies (torch, onnx, onnxconverter-common).")
            sys.exit(1)

    # Ensure onnxruntime_qnn is used (OnnxRuntimeContext checks this env var).
    os.environ.setdefault("QAI_ORT_ONNX_USE_ORTQNN", "1")
    # Default backend: HTP (NPU).
    os.environ.setdefault("QAI_ORTQNN_BACKEND", "htp")
    # Enable FP16 precision on HTP for better performance.
    os.environ.setdefault("QAI_ORTQNN_ENABLE_HTP_FP16", "1")
    # Allow EPContext precompile workaround on layout-transform failures.
    os.environ.setdefault("QAI_ORTQNN_TRY_CONTEXT_CACHE", "1")

    print(f"[INFO] Loading ONNX model via OnnxRuntimeContext: {onnx_path}")
    realesrgan = OnnxRuntimeContext(MODEL_NAME, str(onnx_path), use_cpu)

    provider_mode = realesrgan.getProviderMode()
    print(f"[INFO] OnnxRuntimeContext provider mode: {provider_mode}")
    if provider_mode != "qnn-htp":
        print("[WARN] Model is NOT running on HTP (NPU). "
              "Check that onnxruntime_qnn is installed and the QNN EP is available.")

    _set_image_size_from_model()

# ─────────────────────────────────────────────────────────────────────────────
# Init / Inference / Release
# ─────────────────────────────────────────────────────────────────────────────

def Init(use_cpu: bool = False, use_gpu: bool = False,
         use_bin: bool = False, use_dlc: bool = False,
         use_w8a8: bool = False, use_onnx: bool = False,
         soc_id=None):
    """Initialise the runtime and load the model.

    Parameters
    ----------
    use_cpu   : Use CPU runtime.  Mutually exclusive with use_gpu.
                Always forced True on x86_win.
    use_gpu   : Use GPU runtime.  Ignored on x86_win.
    use_bin   : Explicitly use .bin precompiled HTP context binary (default).
    use_dlc   : Use float .dlc model instead of .bin.
                On x86_win this is always the case regardless of this flag.
    use_w8a8  : Use w8a8 quantised DLC instead of float DLC.
                Only relevant when use_dlc is True.
    use_onnx  : Use models/real_esrgan_x4plus.onnx via OnnxRuntimeContext
                (onnxruntime_qnn HTP EP).  The FP16 ONNX is auto-generated
                if absent.
    soc_id    : SoC ID for hub-model downloader (Linux only).
    """
    global realesrgan

    model_dir.mkdir(parents=True, exist_ok=True)

    model_id = MODEL_ID_WOS if PLATFORM in ("wos", "x86_win") else MODEL_ID_LINUX

    # ── x86_win: always CPU + DLC, ignore --bin / --gpu / --onnx ─────────────
    if PLATFORM == "x86_win":
        if use_gpu:
            print("[WARN] GPU runtime is not supported on x86_win; falling back to CPU.")
        if use_bin:
            print("[WARN] .bin model is not supported on x86_win; using .dlc.")
        if use_onnx:
            print("[WARN] --onnx is not supported on x86_win; using .dlc.")
        use_cpu  = True
        use_bin  = False
        use_onnx = False
        use_dlc  = True   # force DLC on x86_win

    # ── ONNX path: explicit --onnx flag ──────────────────────────────────────
    # When --onnx is requested, always use OnnxRuntimeContext regardless of
    # --cpu / --gpu.  The use_cpu flag is forwarded to OnnxRuntimeContext which
    # selects CPUExecutionProvider (--cpu) or the QNN HTP EP (default).
    if use_onnx:
        onnx_path = model_dir / f"{MODEL_NAME}.onnx"
        if use_cpu:
            print("[INFO] Runtime: CPU (onnxruntime CPUExecutionProvider via OnnxRuntimeContext)")
        elif use_gpu:
            print("[INFO] Runtime: GPU (fallback to QNNExecutionProvider currently)")
        else:
            print("[INFO] Runtime: HTP (onnxruntime_qnn via OnnxRuntimeContext)")
        _init_onnx_htp(onnx_path, use_cpu)
        return

    # ── Decide runtime ────────────────────────────────────────────────────────
    if use_cpu:
        runtime = Runtime.CPU
        print("[INFO] Runtime: CPU")
    elif use_gpu:
        runtime = Runtime.GPU
        print("[INFO] Runtime: GPU")
    else:
        runtime = Runtime.HTP
        print("[INFO] Runtime: HTP")

    # ── Validate runtime / model-format combination ──────────────────────────
    # A .bin model is an HTP-precompiled context binary; it is bound to the
    # Hexagon (NPU) backend and CANNOT be executed on the CPU or GPU backends,
    # which only run float graphs from a .dlc. Reject the invalid combination
    # early with a clear message instead of failing deep inside the runtime.
    if use_bin and (use_cpu or use_gpu):
        backend = "CPU" if use_cpu else "GPU"
        print(
            f"[ERROR] .bin models are HTP (NPU) context binaries and cannot run "
            f"on the {backend} runtime.\n"
            "        The CPU/GPU backends execute float graphs from a .dlc file.\n"
            "        Use one of:\n"
            "          --bin                 (HTP + precompiled .bin)\n"
            f"          --{backend.lower()}  --dlc         (float .dlc on {backend})"
        )
        sys.exit(1)

    if use_w8a8 and (use_cpu or use_gpu):
        backend = "CPU" if use_cpu else "GPU"
        print(
            "[ERROR] w8a8 (INT8) quantised models are only supported on the HTP "
            "(NPU) runtime.\n"
            f"        The {backend} backend runs float graphs and will fail op "
            "validation.\n"
            "        Use one of:\n"
            "          --w8a8                (HTP + quantised .dlc)\n"
            f"          --{backend.lower()}  --dlc         (float .dlc on {backend})"
        )
        sys.exit(1)

    # ── Decide model file ─────────────────────────────────────────────────────
    dlc_suffix = DLC_W8A8_SUFFIX if use_w8a8 else DLC_FLOAT_SUFFIX
    dlc_path   = model_dir / f"{MODEL_NAME}{dlc_suffix}"
    bin_path   = model_dir / f"{MODEL_NAME}.bin"

    if use_bin:
        # User explicitly requested .bin (precompiled HTP context binary)
        if not bin_path.is_file():
            print("[INFO] BIN model not found, downloading via hub...")
            try:
                _download_bin(bin_path, soc_id, model_id)
            except SystemExit:
                # Hub download failed; fall back to float DLC
                print("[INFO] Hub download failed, falling back to float DLC...")
                if not dlc_path.is_file():
                    _download_dlc_float(dlc_path)
                model_path = dlc_path
                print(f"[INFO] Using DLC model (fallback): {model_path}")
                _finish_init(model_path, runtime)
                return
        model_path = bin_path
        print(f"[INFO] Using BIN model: {model_path}")
    else:
        # Default: .dlc (float DLC model)
        if not dlc_path.is_file():
            print(f"[INFO] DLC model not found ({dlc_path.name}), downloading...")
            if use_w8a8:
                _download_dlc_w8a8(dlc_path)
            else:
                _download_dlc_float(dlc_path)
        model_path = dlc_path
        print(f"[INFO] Using DLC model: {model_path}")

    _finish_init(model_path, runtime)


def _finish_init(model_path: Path, runtime):
    """Configure QNN, instantiate the model, and detect IMAGE_SIZE."""
    global realesrgan

    # ── Configure QNN ─────────────────────────────────────────────────────────
    QNNConfig.Config(runtime, LogLevel.WARN, ProfilingLevel.BASIC)

    # ── Instantiate model ─────────────────────────────────────────────────────
    realesrgan = RealESRGan("realesrgan", str(model_path), deviceID=0, coreIdsStr="0")

    # Adapt IMAGE_SIZE from the model's actual input shape
    _set_image_size_from_model()


def Inference(input_image_path, output_image_path, show_image=True, use_cpu=False):
    global image_buffer

    # ── Pre-process ───────────────────────────────────────────────────────────
    print(f"[INFO] Loading input image: {input_image_path}")
    orig_image = Image.open(str(input_image_path)).convert("RGB")
    print(f"[DEBUG] Input image size: {orig_image.size}")

    print(f"[DEBUG] Preprocessing image (resizing to {IMAGE_SIZE}x{IMAGE_SIZE})...")
    image, scale, padding = pil_resize_pad(orig_image, (IMAGE_SIZE, IMAGE_SIZE))

    image = np.array(image, dtype=np.float32)
    image = (np.clip(image, 0, 255) / 255.0).astype(np.float32)

    # Determine tensor layout expected by the model
    input_shapes = realesrgan.getInputShapes()
    layout = None
    if input_shapes and len(input_shapes[0]) == 4:
        layout = _guess_layout_from_shape(input_shapes[0])

    if layout == "NCHW":
        input_tensor = np.transpose(image, (2, 0, 1))[None, ...]
    else:
        input_tensor = image[None, ...]

    input_tensor = np.ascontiguousarray(input_tensor, dtype=np.float32)
    print(f"[DEBUG] input tensor shape : {input_tensor.shape}")

    # ── Run inference ─────────────────────────────────────────────────────────
    is_onnx = isinstance(realesrgan, OnnxRuntimeContext) or (
        hasattr(realesrgan, "isOnnxModel") and realesrgan.isOnnxModel()
    )

    if not is_onnx:
        PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    print(f"[INFO] Starting inference...")
    t0 = time.perf_counter()
    if is_onnx:
        # OnnxRuntimeContext.Inference(input_list) -> list of output arrays
        output_list   = realesrgan.Inference([input_tensor])
        output_tensor = output_list[0]
    else:
        # RealESRGan.Inference(input_data) wraps input in a list internally
        # and returns the first output array directly.
        output_tensor = realesrgan.Inference(input_tensor)
    elapsed = time.perf_counter() - t0
    print(f"[INFO] Inference completed in {elapsed * 1000:.1f} ms ({elapsed:.2f} seconds)")

    if not is_onnx:
        PerfProfile.RelPerfProfileGlobal()
    print(f"[DEBUG] output tensor shape: {getattr(output_tensor, 'shape', None)}")

    # ── Post-process ──────────────────────────────────────────────────────────
    print(f"[INFO] Post-processing output...")
    out = output_tensor
    if isinstance(out, np.ndarray):
        if out.ndim == 4:
            out = out[0]
        if layout == "NCHW" and out.ndim == 3 and out.shape[0] in (1, 3, 4):
            out = np.transpose(out, (1, 2, 0))

    out    = np.clip(out, 0.0, 1.0)
    out_u8 = (out * 255.0).astype(np.uint8)

    output_image = ImageFromArray(out_u8)

    image_size    = (orig_image.size[0] * 4, orig_image.size[1] * 4)
    image_padding = (padding[0] * 4, padding[1] * 4)
    image_buffer  = pil_undo_resize_pad(output_image, image_size, scale, image_padding)

    print(f"[DEBUG] Output image size: {image_buffer.size}")
    print(f"[INFO] Saving output to: {output_image_path}")
    image_buffer.save(str(output_image_path))
    print(f"[INFO] Output saved successfully")

    if show_image:
        print(f"[INFO] Opening image viewer...")
        image_buffer.show()


def Release():
    global realesrgan
    del realesrgan


# ─────────────────────────────────────────────────────────────────────────────
# Debug helpers
# ─────────────────────────────────────────────────────────────────────────────

def getGraphName():
    print("[DEBUG] graph_name     :", realesrgan.getGraphName())

def getInputShapes():
    print("[DEBUG] input_shapes   :", realesrgan.getInputShapes())

def getInputDataType():
    print("[DEBUG] input_dataType :", realesrgan.getInputDataType())

def getOutputShapes():
    print("[DEBUG] output_shapes  :", realesrgan.getOutputShapes())

def getOutputDataType():
    print("[DEBUG] output_dataType:", realesrgan.getOutputDataType())

def getInputName():
    print("[DEBUG] input_name     :", realesrgan.getInputName())

def getOutputName():
    print("[DEBUG] output_name    :", realesrgan.getOutputName())


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def main(input_image_path=None, output_image_path=None,
         show_image=True, use_cpu=False, use_gpu=False,
         use_bin=False, use_dlc=False, use_w8a8=False,
         use_onnx=False, soc_id=None):

    if input_image_path is None:
        input_image_path  = execution_ws / "input.jpg"
    if output_image_path is None:
        output_image_path = execution_ws / "output.png"

    Init(use_cpu=use_cpu, use_gpu=use_gpu,
         use_bin=use_bin, use_dlc=use_dlc,
         use_w8a8=use_w8a8, use_onnx=use_onnx,
         soc_id=soc_id)

    # Print model debug info
    getGraphName()
    getInputShapes()
    getInputDataType()
    getOutputShapes()
    getOutputDataType()
    getInputName()
    getOutputName()

    Inference(
        input_image_path=input_image_path,
        output_image_path=output_image_path,
        show_image=show_image,
        use_cpu=use_cpu,
    )

    Release()
    return "Real ESR Gan Inference Result"


if __name__ == "__main__":
    _platform_note = {
        "wos":         "Windows on Snapdragon (ARM64) -- supports HTP / GPU / CPU",
        "x86_win":     "Windows x86_64 -- CPU only, DLC only",
        "arm64_linux": "ARM64 Linux -- supports HTP / GPU / CPU",
        "x86_linux":   "x86_64 Linux -- supports HTP / GPU / CPU",
        "unknown":     "Unknown platform -- falls back to CPU",
    }.get(PLATFORM, PLATFORM)

    parser = argparse.ArgumentParser(
        description=(
            f"real_esrgan_x4plus unified inference script\n"
            f"Detected platform : {PLATFORM}  ({_platform_note})\n"
            f"Default runtime   : {'CPU (forced)' if PLATFORM == 'x86_win' else 'HTP'}\n"
            f"Default model     : .dlc (float DLC, auto-downloaded)\n"
            f"                    Use --bin for precompiled HTP context binary,\n"
            f"                    --onnx for FP16 ONNX (auto-generated)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--cpu",
        action="store_true",
        help="Use CPU runtime instead of HTP (always active on x86_win)",
    )
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="Use GPU runtime instead of HTP (not supported on x86_win)",
    )
    parser.add_argument(
        "--onnx",
        action="store_true",
        help=(
            "Use models/real_esrgan_x4plus.onnx via OnnxRuntimeContext "
            "(onnxruntime_qnn HTP EP). The FP16 ONNX is auto-generated from "
            "PyTorch weights if the file is absent."
        ),
    )
    parser.add_argument(
        "--bin",
        action="store_true",
        help=(
            "Use .bin precompiled HTP context binary instead of the default .dlc"
        ),
    )
    parser.add_argument(
        "--dlc",
        action="store_true",
        help=(
            "Use float .dlc model (default behaviour; "
            "always active on x86_win; combine with --w8a8 for quantised DLC)"
        ),
    )
    parser.add_argument(
        "--w8a8",
        action="store_true",
        help="Use w8a8 quantised DLC instead of float DLC (requires --dlc)",
    )
    parser.add_argument(
        "--chipset",
        default=None,
        metavar="SOC_ID",
        help="SoC ID for hub-model download (Linux only, e.g. '43')",
    )
    parser.add_argument(
        "--input_image_path",
        default=None,
        help="Path to the input image (default: <script_dir>/input.jpg)",
    )
    parser.add_argument(
        "--output_image_path",
        default=None,
        help="Path to the output image (default: <script_dir>/output.png)",
    )
    parser.add_argument(
        "--no_show",
        action="store_true",
        help="Do not pop up image viewer after inference",
    )

    args = parser.parse_args()

    # --cpu and --gpu are mutually exclusive
    if args.cpu and args.gpu:
        parser.error("--cpu and --gpu are mutually exclusive.")

    # --bin and --dlc are mutually exclusive
    if getattr(args, "bin") and args.dlc:
        parser.error("--bin and --dlc are mutually exclusive.")

    # --w8a8 implies --dlc
    if args.w8a8 and not args.dlc:
        print("[INFO] --w8a8 implies --dlc; switching to DLC mode.")
        args.dlc = True

    main(
        input_image_path  = args.input_image_path,
        output_image_path = args.output_image_path,
        show_image        = not args.no_show,
        use_cpu           = args.cpu,
        use_gpu           = args.gpu,
        use_bin           = getattr(args, "bin"),
        use_dlc           = args.dlc,
        use_w8a8          = args.w8a8,
        use_onnx          = args.onnx,
        soc_id            = args.chipset,
    )
