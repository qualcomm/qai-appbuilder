# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import sys
import os
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(".")
sys.path.append(os.path.join(_SCRIPT_DIR, "..", "..", "..", "common"))   # for image_processing and install

import install
from _image_editing import (
    ImageEditingQNNContext,
    download_model,
    init_htp_model,
    preprocess_for_inpainting,
    postprocess_inpainted_output,
    run_inference_with_perf_profile,
    save_image,
    IMAGE_SIZE,
)
from pathlib import Path

####################################################################

MODEL_ID       = "mn1w65o8m"
MODEL_NAME     = "aotgan"
MODEL_HELP_URL = "https://github.com/qualcomm/qai-appbuilder/blob/main/samples/computerVision/Image_Editing/aotgan/README.md"

####################################################################

execution_ws = Path(_SCRIPT_DIR)
model_dir    = execution_ws / "models"
model_path   = model_dir / "{}.bin".format(MODEL_NAME)

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

####################################################################

aotgan = None


class AotGan(ImageEditingQNNContext):
    """AOT-GAN image inpainting model."""
    pass


def Init():
    global aotgan
    download_model(SOC_ID, MODEL_NAME, model_path, MODEL_HELP_URL)
    aotgan = init_htp_model(model_path, AotGan, "aotgan")


def Inference(input_image_path, input_mask_path, output_image_path):
    image_nhwc, mask_nhwc, orig_image = preprocess_for_inpainting(input_image_path, input_mask_path)
    output_data = run_inference_with_perf_profile(aotgan, image_nhwc, mask_nhwc)
    result_image = postprocess_inpainted_output(output_data, IMAGE_SIZE)
    save_image(result_image, output_image_path, show=True, show_original=orig_image)
    return result_image


def Release():
    global aotgan
    del aotgan


Init()

Inference(execution_ws / "input.png", execution_ws / "mask.png", execution_ws / "output.png")

Release()
