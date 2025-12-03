# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import sys
import os
import argparse
sys.path.append(".")
sys.path.append("python")
import utils.install as install
import numpy as np
from PIL import Image
import torch
from utils.image_processing import (
    preprocess_PIL_image,
    torch_tensor_to_PIL_image,
    pil_resize_pad,
    pil_undo_resize_pad
)

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)
from pathlib import Path

####################################################################

MODEL_ID = "mq218vg7m"
MODEL_NAME = "unet_segmentation"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
IMAGE_SIZE_H = 640
IMAGE_SIZE_W = 1280

####################################################################

execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if not "python" in str(execution_ws):
    execution_ws = execution_ws / "python"

if not MODEL_NAME in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"
model_path = model_dir /  "{}.bin".format(MODEL_NAME)

####################################################################

unet_segmentation = None

# UnetSegmentation class which inherited from the class QNNContext.
class UnetSegmentation(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

def model_download():
    ret = True

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(MODEL_ID, model_path, desc=desc, fail=fail)

    if not ret:
        exit()

def Init():
    global unet_segmentation

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(str(qnn_dir), Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for UnetSegmentation objects.
    unet_segmentation = UnetSegmentation("unet_segmentation", str(model_path))

def Inference(input_image_path, output_image_path, show_image = True): 
    # Read and preprocess the image.
    image = Image.open(input_image_path)
    width, height = image.size
    image, _scale, _padding = pil_resize_pad(image, (IMAGE_SIZE_H, IMAGE_SIZE_W), "reflect")
    image_input = image

    image = preprocess_PIL_image(image).numpy()
    image = np.transpose(image, (0, 2, 3, 1))

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output = unet_segmentation.Inference(image)
    
    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()

    # reshape the output
    output = output.reshape(1, 2, IMAGE_SIZE_H, IMAGE_SIZE_W)
    output = torch.from_numpy(output)
    mask = output.argmax(dim=1)
    mask = mask[0].bool().numpy()

    #save and display the output_image
    mask = Image.fromarray(mask)
    image_size = (width, height)
    image_padding = (_padding[0],_padding[1])

    mask_image = Image.blend(image_input.convert("RGBA"), mask.convert("RGBA"), alpha=1.0)
    mask_image = pil_undo_resize_pad(mask_image,image_size,_scale,image_padding)
    mask_image.save(execution_ws / "output_mask.png")
    if show_image:
        mask_image.show()

    output_image = Image.blend(image_input.convert("RGBA"), mask.convert("RGBA"), alpha=0.5)
    output_image = pil_undo_resize_pad(output_image,image_size,_scale,image_padding)
    output_image.save(output_image_path)
    if show_image:
        output_image.show()


def Release():
    global unet_segmentation

    # Release the resources.
    del(unet_segmentation)

def main(image_path=None,output_path=None,show_image = True):

    if image_path is None:
        # Example image path if no argument is provided
        image_path = execution_ws / "input.jpg"

    if output_path is None:
        output_path = execution_ws / "output.png"


    Init()

    Inference(image_path, output_path, show_image = show_image)

    Release()

    return "Unet Segmentation Inference Result"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a single image path.")
    parser.add_argument('--image', help='Path to the image', default=None)
    parser.add_argument('--output_image', help='Path to image', default=None)

    args = parser.parse_args()
    main(args.image,args.output_image)

