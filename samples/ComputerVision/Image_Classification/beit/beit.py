# ---------------------------------------------------------------------
# Copyright (c) 2025 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
#
# BEiT Image Classification Inference Script
# Vision Transformer model on Snapdragon X Elite / X2 Elite NPU
#

import sys
import os
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(".")
sys.path.append(os.path.join(_SCRIPT_DIR, "..", "..", "..", "common"))   # for install

import install
import argparse
import torch
import torchvision.transforms as transforms
import numpy as np
from pathlib import Path
from PIL import Image
import platform
import json

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

# ─────────────────────────────────────────────────────────────────────────────
# Model Configuration
# ─────────────────────────────────────────────────────────────────────────────

MODEL_NAME = "beit"
MODEL_ID   = "mngvl175n"
HUB_ID_H   = "ox06ibpbkxb4pr0mcyfe7wqgx5pf5r0cm3rf3dzi"
MODEL_HELP_URL = "https://github.com/qualcomm/qai-appbuilder/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"

IMAGENET_CLASSES_URL  = "https://raw.githubusercontent.com/anishathalye/imagenet-simple-labels/master/imagenet-simple-labels.json"
IMAGENET_CLASSES_FILE = "imagenet_labels.json"
INPUT_IMAGE_PATH_URL  = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/imagenet_classifier/v1/dog.jpg"
IMAGE_SIZE = 224

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

execution_ws = Path(os.path.dirname(os.path.abspath(__file__)))
model_dir    = execution_ws / "models"
model_path   = model_dir / "{}.bin".format(MODEL_NAME)
imagenet_classes_path = model_dir / IMAGENET_CLASSES_FILE
input_image_path = execution_ws / "input.jpg"

# ─────────────────────────────────────────────────────────────────────────────
# SOC ID Parsing
# ─────────────────────────────────────────────────────────────────────────────

SOC_ID = None
cleaned_argv = []
i = 0
while i < len(sys.argv):
    if sys.argv[i] == '--chipset':
        SOC_ID = sys.argv[i + 1]
        i += 2
    else:
        cleaned_argv.append(sys.argv[i])
        i += 1

sys.argv = cleaned_argv
print(f"SOC_ID: {SOC_ID}")

# ─────────────────────────────────────────────────────────────────────────────
# Platform Detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_platform():
    """Return one of: 'wos', 'x86_win', 'arm64_linux', 'x86_linux', 'unknown'."""
    system  = platform.system().lower()
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

PLATFORM = _detect_platform()
print(f"[INFO] Detected platform: {PLATFORM}")

# ─────────────────────────────────────────────────────────────────────────────
# Global State
# ─────────────────────────────────────────────────────────────────────────────

beit = None

# ─────────────────────────────────────────────────────────────────────────────
# Preprocessing
# ─────────────────────────────────────────────────────────────────────────────

def preprocess_PIL_image(image: Image.Image) -> torch.Tensor:
    """Convert a PIL image to a normalized tensor of shape (1, C, H, W)."""
    preprocess = transforms.Compose([
        transforms.Resize(IMAGE_SIZE),
        transforms.CenterCrop(IMAGE_SIZE),
        transforms.ToTensor(),
    ])
    img = preprocess(image)
    img = img.unsqueeze(0)
    return img

# ─────────────────────────────────────────────────────────────────────────────
# Post-processing
# ─────────────────────────────────────────────────────────────────────────────

def format_float(num, max_zeros=6):
    if abs(num) >= 1e-6:
        return f"{num:.10f}".rstrip('0').rstrip('.')
    else:
        return f"{num:.{max_zeros+2}f}".rstrip('0').rstrip('.')

def _load_imagenet_labels():
    """Load ImageNet labels from file, with fallback to torchvision built-ins."""
    # Try loading from the downloaded JSON file
    if os.path.exists(imagenet_classes_path) and os.path.getsize(imagenet_classes_path) > 0:
        try:
            with open(imagenet_classes_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            # File is corrupt — delete it so it gets re-downloaded next run
            print(f"[WARN] {imagenet_classes_path} is corrupt, removing it.")
            os.remove(imagenet_classes_path)

    # Fallback: use torchvision's built-in ImageNet class names
    print("[INFO] Using torchvision built-in ImageNet labels as fallback.")
    try:
        from torchvision.models import ResNet50_Weights
        return list(ResNet50_Weights.DEFAULT.meta["categories"])
    except Exception:
        pass

    # Last resort: return numeric class indices as strings
    print("[WARN] Could not load ImageNet labels; using numeric indices.")
    return [str(i) for i in range(1000)]


def post_process(probabilities):
    """Return top-5 classification result string and print it."""
    categories = _load_imagenet_labels()

    top5_prob, top5_catid = torch.topk(probabilities, 5)

    result = "Top 5 predictions for image:\n\n"
    print("Top 5 predictions for image:\n")
    for i in range(top5_prob.size(0)):
        cat_name   = categories[top5_catid[i]]
        item_value = format_float(top5_prob[i].item())
        line = f'"{cat_name}", {item_value}'
        result += line + "\n"
        print(line)

    return result

# ─────────────────────────────────────────────────────────────────────────────
# QNN Model Class
# ─────────────────────────────────────────────────────────────────────────────

class Beit(QNNContext):
    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

# ─────────────────────────────────────────────────────────────────────────────
# Model Download
# ─────────────────────────────────────────────────────────────────────────────

def model_download():
    ret = True

    if not os.path.exists(imagenet_classes_path):
        ret = install.download_url(IMAGENET_CLASSES_URL, imagenet_classes_path)

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"

    if PLATFORM in ("wos", "x86_win"):
        ret = install.download_qai_hubmodel(SOC_ID, MODEL_NAME, model_path, desc=desc, fail=fail, hub_id=HUB_ID_H)
    else:
        ret = install.download_qai_hubmodel(SOC_ID, MODEL_NAME, model_path, desc=desc, fail=fail)

    if not ret:
        exit()

# ─────────────────────────────────────────────────────────────────────────────
# Initialization
# ─────────────────────────────────────────────────────────────────────────────

def Init():
    global beit

    model_download()

    # Configure QNN
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instantiate model
    beit = Beit("beit", str(model_path))

# ─────────────────────────────────────────────────────────────────────────────
# Inference
# ─────────────────────────────────────────────────────────────────────────────

def Inference(image_path):
    """Run inference on image and return top-5 result string."""
    # Read and preprocess
    image = Image.open(image_path).convert("RGB")
    image_tensor = preprocess_PIL_image(image).numpy()
    image_nhwc   = np.transpose(image_tensor, (0, 2, 3, 1))

    # Run inference
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    output_data = beit.Inference(image_nhwc)
    PerfProfile.RelPerfProfileGlobal()

    # Post-process
    output        = torch.from_numpy(output_data).squeeze(0)
    probabilities = torch.softmax(output, dim=0)
    result        = post_process(probabilities)

    return result

# ─────────────────────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────────────────────

def Release():
    global beit
    if beit is not None:
        del beit

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main(input_image=None):
    if input_image is None:
        if not input_image_path.exists():
            install.download_url(INPUT_IMAGE_PATH_URL, input_image_path)
        input_image = input_image_path

    Init()
    Inference(input_image)
    Release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Classify image using BEiT.")
    parser.add_argument('--image', help='Path to the image', default=None)
    args = parser.parse_args()

    main(args.image)
