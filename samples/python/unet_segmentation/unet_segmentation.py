# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import sys
import os
sys.path.append(".")
sys.path.append("..")
import utils.install as install
install.install_qai_appbuilder(install.DEFAULT_SDK_VER)
import numpy as np
import math
from PIL import Image
import torch
from utils.image_processing import (
    preprocess_PIL_image,
    torch_tensor_to_PIL_image,
    pil_resize_pad
)
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

####################################################################

MODEL_ID = "mq218vg7m"
MODEL_NAME = "unet_segmentation"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
IMAGE_SIZE_H = 640
IMAGE_SIZE_W = 1280

####################################################################

execution_ws = os.getcwd()
qnn_dir = execution_ws + "\\qai_libs"

if not MODEL_NAME in execution_ws:
    execution_ws = execution_ws + "\\" + MODEL_NAME

model_dir = execution_ws + "\\models"
madel_path = model_dir + "\\" + MODEL_NAME + ".bin"

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
    ret = install.download_qai_hubmodel(MODEL_ID, madel_path, desc=desc, fail=fail)

    if not ret:
        exit()

def Init():
    global unet_segmentation

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(os.getcwd() + "\\qai_libs", Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for UnetSegmentation objects.
    unet_segmentation = UnetSegmentation("unet_segmentation", madel_path)

def Inference(input_image_path, output_image_path): 
    # Read and preprocess the image.
    image = Image.open(input_image_path)
    image, _, _ = pil_resize_pad(image, (IMAGE_SIZE_H, IMAGE_SIZE_W))
    image_input = image

    image = preprocess_PIL_image(image).numpy()
    image = np.transpose(image, (0, 2, 3, 1))

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output = unet_segmentation.Inference([image])
    
    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()

    # reshape the output
    output = output.reshape(1, 2, IMAGE_SIZE_H, IMAGE_SIZE_W)
    output = torch.from_numpy(output)
    mask = output.argmax(dim=1)
    mask = mask[0].bool().numpy()

    #save and display the output_image
    mask = Image.fromarray(mask)
    mask.save(execution_ws + "\\output_mask.jpg")
    mask.show()
    output_image = Image.blend(image_input.convert("RGBA"), mask.convert("RGBA"), alpha=0.5)
    output_image.save(output_image_path)
    output_image.show()


def Release():
    global unet_segmentation

    # Release the resources.
    del(unet_segmentation)


Init()

Inference(execution_ws + "\\input.jpg", execution_ws + "\\output.png")

Release()

