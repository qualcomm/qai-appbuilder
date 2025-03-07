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
import cv2
import numpy as np
import torch
import torchvision.transforms as transforms
from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from utils.image_processing import (
    preprocess_PIL_image,
    torch_tensor_to_PIL_image,
    pil_resize_pad,
    pil_undo_resize_pad
)
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)


####################################################################

MODEL_ID = "mnz1l2exq"
MODEL_NAME = "real_esrgan_x4plus"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
IMAGE_SIZE = 512

####################################################################

execution_ws = os.getcwd()

if not "python" in execution_ws:
    execution_ws = execution_ws + "\\..\\" + "python"

qnn_dir = execution_ws + "\\qai_libs"

if not MODEL_NAME in execution_ws:
    execution_ws = execution_ws + "\\" + MODEL_NAME

model_dir = execution_ws + "\\models"
madel_path = model_dir + "\\" + MODEL_NAME + ".bin"


####################################################################

image_buffer = None
realesrgan = None


# RealESRGan class which inherited from the class QNNContext.
class RealESRGan(QNNContext):
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
    global realesrgan

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(qnn_dir, Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for RealESRGan objects.
    realesrgan = RealESRGan("realesrgan", madel_path)

def Inference(input_image_path, output_image_path, show_image = True):
    global image_buffer

    # Read and preprocess the image.
    orig_image = Image.open(input_image_path)
    image, scale, padding = pil_resize_pad(orig_image, (IMAGE_SIZE, IMAGE_SIZE))

    image = np.array(image)
    image = np.clip(image, 0, 255) / 255.0  # normalization

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output_image = realesrgan.Inference([image])

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()

    output_image = output_image.reshape(IMAGE_SIZE * 4, IMAGE_SIZE * 4, 3)
    
    output_image = np.clip(output_image, 0.0, 1.0)
    output_image = (output_image * 255).astype(np.uint8) # un-normalization
    output_image = ImageFromArray(output_image)

    image_size = (orig_image.size[0] * 4, orig_image.size[1] * 4)
    image_padding = (padding[0] * 4, padding[1] * 4)
    image_buffer = pil_undo_resize_pad(output_image, image_size, scale, image_padding)

    # show & save the result
    image_buffer.save(output_image_path)
    
    if show_image:
        image_buffer.show()

def Release():
    global realesrgan

    # Release the resources.
    del(realesrgan)

if __name__ == '__main__':
    Init()

    Inference(execution_ws + "\\input.jpg", execution_ws + "\\output.jpg")

    Release()
