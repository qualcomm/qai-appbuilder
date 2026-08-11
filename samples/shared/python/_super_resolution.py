# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ─────────────────────────────────────────────────────────────────────────────
#
# Shared utilities for Super Resolution model inference scripts.
# Provides platform detection, model download, QNN initialization, image
# preprocessing/postprocessing, and common CLI argument parsing.
#

import sys
import os
import platform
import argparse
import urllib.request
import zipfile
import shutil
import numpy as np
from pathlib import Path
from PIL import Image
from PIL.Image import fromarray as ImageFromArray

import install
from image_processing import pil_resize_pad, pil_undo_resize_pad
from qai_appbuilder import (
    QNNContext,
    Runtime,
    LogLevel,
    ProfilingLevel,
    PerfProfile,
    QNNConfig,
)


# ─────────────────────────────────────────────────────────────────────────────
# Platform detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_platform():
    """Return one of: 'wos', 'x86_win', 'arm64_linux', 'x86_linux', 'unknown'."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        if machine in ("aarch64", "arm64"):
            return "wos"
        else:
            return "x86_win"
    if system == "linux":
        if machine in ("aarch64", "arm64"):
            return "arm64_linux"
        if machine in ("x86_64", "amd64"):
            return "x86_linux"
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Model download helpers
# ─────────────────────────────────────────────────────────────────────────────

def download_dlc(dlc_path: Path, url: str, zip_filename: str, work_dir: Path):
    """Download and extract a DLC zip to dlc_path if it does not already exist.

    Parameters
    ----------
    dlc_path     : destination path for the extracted .dlc file
    url          : HTTPS URL of the zip archive
    zip_filename : local filename to use while downloading the zip
    work_dir     : working directory for temporary files
    """
    if dlc_path.is_file():
        print(f"[INFO] DLC model already exists: {dlc_path}")
        return

    zip_path = work_dir / zip_filename

    print(f"[INFO] Downloading DLC model from:\n  {url}")
    try:
        urllib.request.urlretrieve(url, str(zip_path))
        print(f"[INFO] Download complete: {zip_path}")
    except Exception as e:
        print(f"[ERROR] Failed to download DLC model: {e}")
        sys.exit(1)

    extract_dir = work_dir / "_dlc_extract_tmp"
    print(f"[INFO] Extracting {zip_filename} …")
    try:
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            zf.extractall(str(extract_dir))
    except Exception as e:
        print(f"[ERROR] Failed to extract zip: {e}")
        zip_path.unlink(missing_ok=True)
        sys.exit(1)

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

    dlc_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(found_dlc), str(dlc_path))
    print(f"[INFO] Copied DLC to: {dlc_path}")

    shutil.rmtree(str(extract_dir), ignore_errors=True)
    zip_path.unlink(missing_ok=True)
    print(f"[INFO] Removed temporary zip: {zip_path}")


def download_bin(bin_path: Path, model_name: str, model_id: str, soc_id: str,
                 platform_str: str, help_url: str, hub_id=None) -> bool:
    """Download .bin model via QAI Hub if bin_path does not exist.

    Parameters
    ----------
    bin_path      : destination path for the .bin file
    model_name    : model name (e.g., "real_esrgan_x4plus")
    model_id      : hub model ID for WoS/x86_win
    soc_id        : SoC ID for Linux platforms
    platform_str  : platform string ('wos', 'x86_win', 'arm64_linux', 'x86_linux', 'unknown')
    help_url      : help URL to display on failure
    hub_id        : optional hub API token (passed to install.download_qai_hubmodel)

    Returns
    -------
    bool : True on success, False on failure (does NOT call sys.exit)
    """
    if bin_path.is_file():
        print(f"[INFO] BIN model already exists: {bin_path}")
        return True

    bin_path.parent.mkdir(parents=True, exist_ok=True)

    desc = f"Downloading {model_name} model… "
    fail = (
        f"\nFailed to download {model_name} model. "
        f"Please prepare the model according to:\n{help_url}"
    )

    kwargs = {"desc": desc, "fail": fail}
    if hub_id is not None:
        kwargs["hub_id"] = hub_id

    if platform_str in ("wos", "x86_win"):
        ret = install.download_qai_hubmodel(model_id, str(bin_path), **kwargs)
    else:
        ret = install.download_qai_hubmodel(soc_id, model_name, str(bin_path), **kwargs)

    return ret


# ─────────────────────────────────────────────────────────────────────────────
# Layout helpers
# ─────────────────────────────────────────────────────────────────────────────

def guess_layout_from_shape(shape4):
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


def get_image_size_from_model(model_instance, default=128) -> int:
    """Get the expected input image size from the model's input shape.

    Parameters
    ----------
    model_instance : QNNContext instance
    default        : fallback size if detection fails

    Returns
    -------
    int : detected or default image size
    """
    try:
        shapes = model_instance.getInputShapes()
        if shapes and len(shapes[0]) == 4:
            layout = guess_layout_from_shape(shapes[0])
            size = int(shapes[0][2]) if layout == "NCHW" else int(shapes[0][1])
            print(f"[INFO] Detected input layout : {layout}")
            print(f"[INFO] Using IMAGE_SIZE       : {size}")
            return size
    except Exception as e:
        print(f"[WARN] Failed to infer IMAGE_SIZE from model input shape: {e}")
    return default


# ─────────────────────────────────────────────────────────────────────────────
# QNN model base class
# ─────────────────────────────────────────────────────────────────────────────

class SuperResolutionQNNContext(QNNContext):
    """Base class for Super Resolution QNN models with simplified Inference."""

    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# ─────────────────────────────────────────────────────────────────────────────
# Image preprocessing
# ─────────────────────────────────────────────────────────────────────────────

def preprocess_image_for_sr(input_image_path, image_size, layout=None):
    """Preprocess image for Super Resolution inference.

    Opens image, resizes/pads to image_size, normalizes to [0, 1], and adds batch dimension.

    Parameters
    ----------
    input_image_path : path to input image
    image_size       : target size (H, W) or scalar
    layout           : optional layout hint ('NCHW' or 'NHWC'); if 'NCHW', transposes to NCHW

    Returns
    -------
    tuple : (orig_image, input_tensor, scale, padding)
        - orig_image: PIL Image (original, before resize/pad)
        - input_tensor: float32 ndarray, shape (1, H, W, 3) or (1, 3, H, W)
        - scale: float, scale factor applied
        - padding: (pad_left, pad_top) tuple
    """
    if isinstance(image_size, int):
        image_size = (image_size, image_size)

    orig_image = Image.open(str(input_image_path)).convert("RGB")
    image, scale, padding = pil_resize_pad(orig_image, image_size)

    image = np.array(image, dtype=np.float32)
    image = (np.clip(image, 0, 255) / 255.0).astype(np.float32)

    if layout == "NCHW":
        input_tensor = np.transpose(image, (2, 0, 1))[None, ...]
    else:
        input_tensor = image[None, ...]

    input_tensor = np.ascontiguousarray(input_tensor, dtype=np.float32)
    return orig_image, input_tensor, scale, padding


# ─────────────────────────────────────────────────────────────────────────────
# Image postprocessing
# ─────────────────────────────────────────────────────────────────────────────

def postprocess_sr_output(output_tensor, orig_image, scale, padding,
                          upscale_factor=4, expected_hw=None, layout=None):
    """Postprocess Super Resolution model output.

    Handles tensor reshaping, layout conversion, normalization, and inverse resize/pad.

    Parameters
    ----------
    output_tensor    : model output (ndarray or tensor)
    orig_image       : original PIL Image (before resize/pad)
    scale            : scale factor from preprocessing
    padding          : (pad_left, pad_top) from preprocessing
    upscale_factor   : upscaling factor (default 4)
    expected_hw      : optional (H, W) for reshaping flat outputs
    layout           : optional layout hint ('NCHW' or 'NHWC')

    Returns
    -------
    PIL.Image : final output image
    """
    out = output_tensor
    if isinstance(out, np.ndarray):
        if out.ndim == 1 and expected_hw is not None:
            out = out.reshape(expected_hw[0], expected_hw[1], 3)
        elif out.ndim == 4:
            out = out[0]
        if layout == "NCHW" and out.ndim == 3 and out.shape[0] in (1, 3, 4):
            out = np.transpose(out, (1, 2, 0))

    out = np.clip(out, 0.0, 1.0)
    out_u8 = (out * 255.0).astype(np.uint8)

    output_image = ImageFromArray(out_u8)

    image_size = (orig_image.size[0] * upscale_factor, orig_image.size[1] * upscale_factor)
    image_padding = (padding[0] * upscale_factor, padding[1] * upscale_factor)
    result = pil_undo_resize_pad(output_image, image_size, scale, image_padding)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Performance profiling
# ─────────────────────────────────────────────────────────────────────────────

def run_with_perf_profile(model_instance, input_tensor):
    """Run inference with HTP burst performance profile.

    Parameters
    ----------
    model_instance : QNN model instance
    input_tensor   : input data

    Returns
    -------
    ndarray : model output
    """
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    output = model_instance.Inference(input_tensor)
    PerfProfile.RelPerfProfileGlobal()
    return output


# ─────────────────────────────────────────────────────────────────────────────
# Debug helpers
# ─────────────────────────────────────────────────────────────────────────────

def print_model_debug_info(model_instance):
    """Print model metadata (graph name, shapes, dtypes, names)."""
    try:
        print("[DEBUG] graph_name     :", model_instance.getGraphName())
    except:
        pass
    try:
        print("[DEBUG] input_shapes   :", model_instance.getInputShapes())
    except:
        pass
    try:
        print("[DEBUG] input_dataType :", model_instance.getInputDataType())
    except:
        pass
    try:
        print("[DEBUG] output_shapes  :", model_instance.getOutputShapes())
    except:
        pass
    try:
        print("[DEBUG] output_dataType:", model_instance.getOutputDataType())
    except:
        pass
    try:
        print("[DEBUG] input_name     :", model_instance.getInputName())
    except:
        pass
    try:
        print("[DEBUG] output_name    :", model_instance.getOutputName())
    except:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# CLI argument parsing
# ─────────────────────────────────────────────────────────────────────────────

def build_arg_parser(model_name: str, platform_str: str, description: str = "") -> argparse.ArgumentParser:
    """Build argument parser for Super Resolution inference scripts.

    Parameters
    ----------
    model_name   : model name (e.g., "real_esrgan_x4plus")
    platform_str : platform string (e.g., "wos", "x86_win")
    description  : optional description prefix

    Returns
    -------
    argparse.ArgumentParser : configured parser
    """
    platform_note = {
        "wos": "Windows on Snapdragon (ARM64) — supports HTP / GPU / CPU",
        "x86_win": "Windows x86_64 — CPU only, DLC only",
        "arm64_linux": "ARM64 Linux — supports HTP / GPU / CPU",
        "x86_linux": "x86_64 Linux — supports HTP / GPU / CPU",
        "unknown": "Unknown platform — falls back to CPU",
    }.get(platform_str, platform_str)

    default_runtime = "CPU (forced)" if platform_str == "x86_win" else "HTP"

    full_description = (
        f"{model_name} unified inference script\n"
        f"Detected platform : {platform_str}  ({platform_note})\n"
        f"Default runtime   : {default_runtime}\n"
        f"Default model     : float .dlc (auto-downloaded if absent)"
    )
    if description:
        full_description = description + "\n" + full_description

    parser = argparse.ArgumentParser(
        description=full_description,
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

    return parser
