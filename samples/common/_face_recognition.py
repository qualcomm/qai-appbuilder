# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ─────────────────────────────────────────────────────────────────────────────
#
# Shared utilities for Face Recognition model inference scripts.
# Provides model download, QNN initialization, image preprocessing, and inference helpers
# for face attribute and 3D face mapping models.
#

import os
import json
import numpy as np
import platform
from pathlib import Path
from PIL import Image

from qai_appbuilder import (
    QNNContext,
    Runtime,
    LogLevel,
    ProfilingLevel,
    PerfProfile,
    QNNConfig,
)
import install


# ─────────────────────────────────────────────────────────────────────────────
# Platform Detection
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
# QNN Context Classes
# ─────────────────────────────────────────────────────────────────────────────

class FaceRecognitionQNNContext(QNNContext):
    """Base QNN context for face recognition models (NHWC, float32 input, multi-output)."""

    def Inference(self, input_data):
        return super().Inference([input_data])


# ─────────────────────────────────────────────────────────────────────────────
# Model Download Helpers
# ─────────────────────────────────────────────────────────────────────────────

def download_model(soc_id, model_name, model_path, help_url, hub_id=None) -> bool:
    """Download a QAI hub model .bin; returns True if successful."""
    model_path = Path(model_path)
    if model_path.is_file():
        return True
    model_path.parent.mkdir(parents=True, exist_ok=True)
    desc = f"Downloading {model_name} model... "
    fail = f"\nFailed to download {model_name} model. Please prepare the model according to the steps in below link:\n{help_url}"
    kwargs = {"desc": desc, "fail": fail}
    if hub_id:
        kwargs["hub_id"] = hub_id
    return install.download_qai_hubmodel(soc_id, model_name, str(model_path), **kwargs)


def download_asset(url: str, local_path) -> bool:
    """Download an asset file (npy, txt, etc.) if not already present."""
    local_path = Path(local_path)
    if local_path.is_file():
        return True
    local_path.parent.mkdir(parents=True, exist_ok=True)
    return install.download_url(url, str(local_path))


# ─────────────────────────────────────────────────────────────────────────────
# QNN Configuration
# ─────────────────────────────────────────────────────────────────────────────

def init_htp_model(model_path, model_class, instance_name):
    """Configure HTP runtime and instantiate model."""
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
    return model_class(instance_name, str(model_path))


# ─────────────────────────────────────────────────────────────────────────────
# Image Preprocessing
# ─────────────────────────────────────────────────────────────────────────────

def preprocess_face_image(image_path, image_size=128, normalize=True):
    """Preprocess face image: load, resize, normalize to NHWC float32."""
    image = Image.open(str(image_path)).convert("RGB")
    image = image.resize((image_size, image_size), Image.BILINEAR)
    img_array = np.array(image, dtype=np.float32)

    if normalize:
        img_array = img_array / 255.0

    img_array = img_array[np.newaxis, ...]  # Add batch dimension
    return img_array


# ─────────────────────────────────────────────────────────────────────────────
# Inference Helpers
# ─────────────────────────────────────────────────────────────────────────────

def run_inference_with_perf_profile(model_instance, input_data):
    """Run inference wrapped in HTP BURST perf profile."""
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    output = model_instance.Inference(input_data)
    PerfProfile.RelPerfProfileGlobal()
    return output


# ─────────────────────────────────────────────────────────────────────────────
# Output Processing
# ─────────────────────────────────────────────────────────────────────────────

def save_face_attributes_json(output_data, output_names, output_path):
    """Save face attribute outputs to JSON file."""
    pred_res_list = [np.squeeze(out) for out in output_data]
    out_dict = {}
    for i, name in enumerate(output_names):
        out_dict[name] = list(pred_res_list[i].astype(float))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out_dict, f, ensure_ascii=False, indent=4)

    print(f"Model outputs saved to: {output_path}")
    return out_dict


def save_image(image: Image.Image, base_dir, filename, desc):
    """Save a PIL image and open it for preview."""
    os.makedirs(base_dir, exist_ok=True)
    filepath = os.path.join(base_dir, filename)
    image.save(filepath)
    print(f"Saving {desc} to {filepath}")
    try:
        image.show()
    except Exception:
        pass  # Image.show() may not work in headless environments
