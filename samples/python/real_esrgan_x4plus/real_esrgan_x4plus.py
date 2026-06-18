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
# Default model   : float .dlc  (auto-downloaded if absent)
#
# CLI options:
#   --cpu              Use CPU runtime instead of HTP
#   --gpu              Use GPU runtime instead of HTP
#   --bin              Prefer .bin model file instead of .dlc
#                      (ignored on x86_win — always uses .dlc)
#   --w8a8             Use w8a8 quantised DLC instead of float DLC
#   --chipset <id>     Override SoC ID used for hub-model download
#   --input_image_path <path>   Input image  (default: <script_dir>/input.jpg)
#   --output_image_path <path>  Output image (default: <script_dir>/output.png)
#   --no_show          Do not pop up image viewer after inference
# ---------------------------------------------------------------------

import sys
import os
import platform
import argparse
import urllib.request
import zipfile
import shutil

sys.path.append(".")
sys.path.append("python")

import utils.install as install
import numpy as np
from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from utils.image_processing import (
    pil_resize_pad,
    pil_undo_resize_pad,
)
from qai_appbuilder import (
    QNNContext,
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

# DLC file name suffixes stored on disk
DLC_FLOAT_SUFFIX = ".dlc"           # models/real_esrgan_x4plus.dlc
DLC_W8A8_SUFFIX  = "-w8a8.dlc"     # models/real_esrgan_x4plus-w8a8.dlc

# Fallback image size; overridden at runtime from model input shape when possible.
IMAGE_SIZE = 128

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
        output_data = super().Inference(input_datas)[0]
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
    """Update IMAGE_SIZE using the model's expected input shape."""
    global IMAGE_SIZE
    try:
        shapes = realesrgan.getInputShapes()
        if shapes and len(shapes[0]) == 4:
            layout = _guess_layout_from_shape(shapes[0])
            IMAGE_SIZE = int(shapes[0][2]) if layout == "NCHW" else int(shapes[0][1])
            print(f"[INFO] Detected input layout : {layout}")
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

    # WoS / x86_win use (model_id, path); Linux uses (soc_id, name, path)
    if PLATFORM in ("wos", "x86_win"):
        ret = install.download_qai_hubmodel(model_id, str(bin_path), desc=desc, fail=fail)
    else:
        ret = install.download_qai_hubmodel(soc_id, MODEL_NAME, str(bin_path), desc=desc, fail=fail)

    if not ret:
        sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Init / Inference / Release
# ─────────────────────────────────────────────────────────────────────────────

def Init(use_cpu: bool = False, use_gpu: bool = False,
         use_bin: bool = False, use_w8a8: bool = False,
         soc_id=None):
    """Initialise the QNN runtime and load the model.

    Parameters
    ----------
    use_cpu  : Use CPU runtime.  Mutually exclusive with use_gpu.
               Always forced True on x86_win.
    use_gpu  : Use GPU runtime.  Ignored on x86_win.
    use_bin  : Prefer .bin model file.  Ignored on x86_win (always .dlc).
    use_w8a8 : Use w8a8 quantised DLC instead of float DLC.
               Only relevant when a DLC is being used.
    soc_id   : SoC ID for hub-model downloader (Linux only).
    """
    global realesrgan

    model_dir.mkdir(parents=True, exist_ok=True)

    model_id = MODEL_ID_WOS if PLATFORM in ("wos", "x86_win") else MODEL_ID_LINUX

    # ── x86_win: always CPU + DLC, ignore --bin / --gpu ──────────────────────
    if PLATFORM == "x86_win":
        if use_gpu:
            print("[WARN] GPU runtime is not supported on x86_win; falling back to CPU.")
        if use_bin:
            print("[WARN] .bin model is not supported on x86_win; using .dlc.")
        use_cpu = True
        use_bin = False

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
            f"          --{backend.lower()}                 (float .dlc, drop --bin)"
        )
        sys.exit(1)

    # w8a8 (INT8) quantised models are only supported on the HTP (NPU) runtime.
    # The GPU/CPU backends run float graphs and will fail op validation.
    if use_w8a8 and (use_cpu or use_gpu):
        backend = "CPU" if use_cpu else "GPU"
        print(
            "[ERROR] w8a8 (INT8) quantised models are only supported on the HTP "
            "(NPU) runtime.\n"
            f"        The {backend} backend runs float graphs and will fail op "
            "validation.\n"
            "        Use one of:\n"
            "          --w8a8                (HTP + quantised .dlc)\n"
            f"          --{backend.lower()}                 (float .dlc, drop --w8a8)"
        )
        sys.exit(1)

    # ── Decide model file ─────────────────────────────────────────────────────
    dlc_suffix = DLC_W8A8_SUFFIX if use_w8a8 else DLC_FLOAT_SUFFIX
    dlc_path   = model_dir / f"{MODEL_NAME}{dlc_suffix}"
    bin_path   = model_dir / f"{MODEL_NAME}.bin"

    if use_bin:
        # User explicitly requested .bin
        if not bin_path.is_file():
            print("[INFO] BIN model not found, downloading via hub…")
            _download_bin(bin_path, soc_id, model_id)
        model_path = bin_path
        print(f"[INFO] Using BIN model: {model_path}")
    else:
        # Default: DLC (float or w8a8)
        if not dlc_path.is_file():
            print(f"[INFO] DLC model not found ({dlc_path.name}), downloading…")
            if use_w8a8:
                _download_dlc_w8a8(dlc_path)
            else:
                # For non-x86_win platforms try hub download first (.bin),
                # then fall back to the public DLC zip.
                if PLATFORM not in ("wos", "x86_win") and not use_w8a8:
                    if not bin_path.is_file():
                        print("[INFO] Attempting hub download for .bin first…")
                        try:
                            _download_bin(bin_path, soc_id, model_id)
                            model_path = bin_path
                            print(f"[INFO] Hub download succeeded, using BIN: {model_path}")
                            # Skip DLC download — jump straight to QNNConfig
                            _finish_init(model_path, runtime)
                            return
                        except SystemExit:
                            print("[INFO] Hub download failed, falling back to public DLC zip…")
                    else:
                        model_path = bin_path
                        print(f"[INFO] DLC not found but BIN exists, using BIN: {model_path}")
                        _finish_init(model_path, runtime)
                        return
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
    orig_image = Image.open(str(input_image_path)).convert("RGB")
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
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    output_tensor = realesrgan.Inference(input_tensor)
    PerfProfile.RelPerfProfileGlobal()

    print(f"[DEBUG] output tensor shape: {getattr(output_tensor, 'shape', None)}")

    # ── Post-process ──────────────────────────────────────────────────────────
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

    print(f"[INFO] Saving output to: {output_image_path}")
    image_buffer.save(str(output_image_path))

    if show_image:
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
         use_bin=False, use_w8a8=False, soc_id=None):

    if input_image_path is None:
        input_image_path  = execution_ws / "input.jpg"
    if output_image_path is None:
        output_image_path = execution_ws / "output.png"

    Init(use_cpu=use_cpu, use_gpu=use_gpu,
         use_bin=use_bin, use_w8a8=use_w8a8, soc_id=soc_id)

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
        "wos":        "Windows on Snapdragon (ARM64) — supports HTP / GPU / CPU",
        "x86_win":    "Windows x86_64 — CPU only, DLC only",
        "arm64_linux":"ARM64 Linux — supports HTP / GPU / CPU",
        "x86_linux":  "x86_64 Linux — supports HTP / GPU / CPU",
        "unknown":    "Unknown platform — falls back to CPU",
    }.get(PLATFORM, PLATFORM)

    parser = argparse.ArgumentParser(
        description=(
            f"real_esrgan_x4plus unified inference script\n"
            f"Detected platform : {PLATFORM}  ({_platform_note})\n"
            f"Default runtime   : {'CPU (forced)' if PLATFORM == 'x86_win' else 'HTP'}\n"
            f"Default model     : float .dlc (auto-downloaded if absent)"
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
        "--bin",
        action="store_true",
        help="Prefer .bin model file instead of .dlc (ignored on x86_win)",
    )
    parser.add_argument(
        "--w8a8",
        action="store_true",
        help="Use w8a8 quantised DLC instead of float DLC",
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

    main(
        input_image_path  = args.input_image_path,
        output_image_path = args.output_image_path,
        show_image        = not args.no_show,
        use_cpu           = args.cpu,
        use_gpu           = args.gpu,
        use_bin           = getattr(args, "bin"),
        use_w8a8          = args.w8a8,
        soc_id            = args.chipset,
    )
