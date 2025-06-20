# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import sys
import os
sys.path.append(".")
sys.path.append("python")
import utils.install as install
import numpy as np
import torch
import torchvision.transforms as transforms

from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from utils.image_processing import (
    preprocess_inputs
)
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

####################################################################

MODEL_ID = "mn1w65o8m"
MODEL_NAME = "aotgan"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
IMAGE_SIZE = 512

####################################################################

execution_ws = os.getcwd()
qnn_dir = execution_ws + "\\qai_libs"

if not "python" in execution_ws:
    execution_ws = execution_ws + "\\" + "python"

if not MODEL_NAME in execution_ws:
    execution_ws = execution_ws + "\\" + MODEL_NAME

model_dir = execution_ws + "\\models"
model_path = model_dir + "\\" + MODEL_NAME + ".bin"

####################################################################

image_buffer = None
aotgan = None

def preprocess_PIL_image(image: Image) -> torch.Tensor:
    """Convert a PIL image into a pyTorch tensor with range [0, 1] and shape NCHW."""
    transform = transforms.Compose([transforms.Resize(IMAGE_SIZE),      # bgr image
                                    transforms.CenterCrop(IMAGE_SIZE),
                                    transforms.PILToTensor()])
    img: torch.Tensor = transform(image)  # type: ignore
    img = img.float().unsqueeze(0) / 255.0  # int 0 - 255 to float 0.0 - 1.0
    return img

def torch_tensor_to_PIL_image(data: torch.Tensor) -> Image:
    """
    Convert a Torch tensor (dtype float32) with range [0, 1] and shape CHW into PIL image CHW
    """
    out = torch.clip(data, min=0.0, max=1.0)
    np_out = (out.detach().numpy() * 255).astype(np.uint8)
    return ImageFromArray(np_out)

# LamaDilated class which inherited from the class QNNContext.
class AotGan(QNNContext):
    def Inference(self, input_data, input_mask):
        input_datas=[input_data, input_mask]
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
    global aotgan

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(os.getcwd() + "\\qai_libs", Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for AotGan objects.
    aotgan = AotGan("aotgan", model_path)

def Inference(input_image_path, input_mask_path, output_image_path):
    global image_buffer

    # Read and preprocess the image & mask.
    image = Image.open(input_image_path)
    mask = Image.open(input_mask_path)   
    inputs = preprocess_inputs(image, mask)
    image_masked, mask_torch = inputs["image"], inputs["mask"]         
    image_masked = image_masked.numpy()
    mask_torch = mask_torch.numpy()
     
    image_masked = np.transpose(image_masked, (0, 2, 3, 1)) 
    mask_torch = np.transpose(mask_torch, (0, 2, 3, 1)) 

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output_image = aotgan.Inference([image_masked], [mask_torch])

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()

    # show & save the result
    output_image = torch.from_numpy(output_image)
    output_image = output_image.reshape(IMAGE_SIZE, IMAGE_SIZE, 3)
    output_image = torch.unsqueeze(output_image, 0)
    output_image = [torch_tensor_to_PIL_image(img) for img in output_image]
    image_buffer = output_image[0]
    image_buffer.save(output_image_path)
    image_buffer.show()
    image.show()

def Release():
    global aotgan

    # Release the resources.
    del(aotgan)


Init()

Inference(execution_ws + "\\input.png", execution_ws + "\\mask.png", execution_ws + "\\output.png")

Release()

