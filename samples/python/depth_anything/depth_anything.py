# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
sys.path.append(".")
sys.path.append("python")
import utils.install as install
import cv2
from typing import cast
import numpy as np
import numpy.typing as npt
import torch
import torchvision.transforms as transforms
from PIL import Image
from PIL.Image import fromarray as ImageFromArray
import argparse
import matplotlib.pyplot as plt


from utils.image_processing import pil_resize_pad, undo_resize_pad
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)
from pathlib import Path

####################################################################

MODEL_ID = "mnw5x9kpn"
MODEL_NAME = "depth_anything"
MODEL_HELP_URL = ""
IMAGE_SIZE = 518

####################################################################

execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if not "python" in str(execution_ws):
    execution_ws = execution_ws / "python"

if not MODEL_NAME in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"
model_path = model_dir /  "{}.bin".format(MODEL_NAME)

input_image_path = execution_ws / "input.jpg"
INPUT_IMAGE_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/depth_anything/v2/test_input_image.jpg"
out_image_path = execution_ws
####################################################################


depth_anything = None

class DepthAnything(QNNContext):
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
    global depth_anything

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(str(qnn_dir), Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for DepthAnything objects.
    depth_anything = DepthAnything("depth_anything", str(model_path))



def Inference(input_image_path):
    global out_image_path
    # Read and preprocess the image.
    original_image = Image.open(input_image_path)
    image,scale,padding = preprocess_image(original_image)
    image = np.transpose(image, (0, 2, 3, 1))

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output_data = depth_anything.Inference(image)

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()
    
    # Post process
    output_image = post_process(output_data,original_image.size,scale,padding)

    # Save and show image

    if not os.path.exists(out_image_path):
            os.makedirs(out_image_path, exist_ok=True)
    out_image_path = out_image_path / "output.jpg"
    output_image.save(out_image_path)
    output_image.show()
    
    return output_image
def Release():
    global depth_anything

    # Release the resources.
    del(depth_anything)

def main(input = None):

    if input is None:
        if not os.path.exists(input_image_path):
            ret = True
            ret = install.download_url(INPUT_IMAGE_PATH_URL, input_image_path)
        input = input_image_path

    Init()

    result = Inference(input)

    Release()

    return result


def preprocess_image(image:Image.Image):
    resized_image, scale, padding = pil_resize_pad(
            image, (IMAGE_SIZE, IMAGE_SIZE)
        )
    image_tensor = transforms.ToTensor()(resized_image).unsqueeze(0)
    return image_tensor.numpy(),scale,padding
   

def post_process(prediction,original_size,scale,padding):          
    prediction = torch.tensor(prediction)  
    prediction = prediction.reshape( 1, 1,518, 518)
    prediction = undo_resize_pad(prediction, original_size, scale, padding)
    numpy_output = cast(npt.NDArray[np.float32], prediction.squeeze().cpu().numpy())
    heatmap = plt.cm.plasma(numpy_output / numpy_output.max())[..., :3]  # type: ignore[attr-defined]
    out_image = Image.fromarray((heatmap * 255).astype(np.uint8))

    return out_image


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a single image path.")
    parser.add_argument('--image', help='Path to the image', default=None)
    args = parser.parse_args()

    main(args.image)


