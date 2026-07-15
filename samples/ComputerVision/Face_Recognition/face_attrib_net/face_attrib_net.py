# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ─────────────────────────────────────────────────────────────────────────────
#
# Face Attribute Net Inference Script
# Face attribute detection on Snapdragon X Elite / X2 Elite NPU
#

import sys
import os
sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "common"))

import install
import argparse
import json
import numpy as np
from pathlib import Path
from PIL import Image

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)
from _face_recognition import (
    FaceRecognitionQNNContext,
    download_model,
    init_htp_model,
    preprocess_face_image,
    run_inference_with_perf_profile,
    save_face_attributes_json,
)

# ─────────────────────────────────────────────────────────────────────────────
# Model Configuration
# ─────────────────────────────────────────────────────────────────────────────

MODEL_ID = "mnj1jvgdn"
MODEL_NAME = "face_attrib_net"
MODEL_HELP_URL = "https://github.com/qualcomm/qai-appbuilder/blob/main/samples/computerVision/Face_Recognition/face_attrib_net/README.md"
IMAGE_SIZE = 128

OUTPUT_NAMES = [
    "id_feature",
    "liveness_feature",
    "eye_closeness",
    "glasses",
    "mask",
    "sunglasses",
]

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

execution_ws = Path(os.path.dirname(os.path.abspath(__file__)))
model_dir = execution_ws / "models"
model_path = model_dir / f"{MODEL_NAME}.bin"
output_dir = execution_ws

input_face_image_path = execution_ws / "input.bmp"
INPUT_FACE_IMAGE_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/face_attrib_net/v1/img_sample.bmp"

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
# Global State
# ─────────────────────────────────────────────────────────────────────────────

face_attrib_net = None


# ─────────────────────────────────────────────────────────────────────────────
# QNN Model Class
# ─────────────────────────────────────────────────────────────────────────────

class FaceAttribNet(FaceRecognitionQNNContext):
    """Face Attribute Net QNN model wrapper."""

    def Inference(self, input_data):
        return super().Inference(input_data)


# ─────────────────────────────────────────────────────────────────────────────
# Initialization
# ─────────────────────────────────────────────────────────────────────────────

def Init():
    """Initialize model."""
    global face_attrib_net

    # Download model
    if not download_model(SOC_ID, MODEL_NAME, model_path, MODEL_HELP_URL):
        sys.exit(1)

    # Configure QNN
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instantiate model
    face_attrib_net = init_htp_model(model_path, FaceAttribNet, "face_attrib_net")


# ─────────────────────────────────────────────────────────────────────────────
# Inference
# ─────────────────────────────────────────────────────────────────────────────

def Inference(input_image_path):
    """Run inference on face image."""
    # Preprocess image
    input_image = preprocess_face_image(input_image_path, IMAGE_SIZE, normalize=True)

    # Run inference with performance profile
    raw_output = run_inference_with_perf_profile(face_attrib_net, input_image)

    # Save outputs
    output_file = output_dir / "output.json"
    save_face_attributes_json(raw_output, OUTPUT_NAMES, output_file)

    return raw_output


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────────────────────

def Release():
    """Release model resources."""
    global face_attrib_net
    if face_attrib_net is not None:
        del face_attrib_net


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main(input_image=None):
    """Main entry point."""
    if input_image is None:
        if not input_face_image_path.exists():
            print(f"Downloading sample image...")
            install.download_url(INPUT_FACE_IMAGE_PATH_URL, input_face_image_path)
        input_image = input_face_image_path

    Init()
    Inference(input_image)
    Release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Detect face attributes.")
    parser.add_argument('--image', help='Path to face image', default=None)
    args = parser.parse_args()

    main(args.image)
