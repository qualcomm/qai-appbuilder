# ---------------------------------------------------------------------
# Copyright (c) 2025 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
sys.path.append(".")
sys.path.append("python")
import utils.install as install
import cv2
import json
import numpy as np
import torch
import torchvision.transforms as transforms
from PIL import Image
from PIL.Image import fromarray as ImageFromArray
import argparse

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)
from pathlib import Path

####################################################################
MODEL_ID = "mnw5x8wrn"
MODEL_NAME = "googlenet"
MODEL_HELP_URL = ""
IMAGENET_CLASSES_URL = "https://raw.githubusercontent.com/anishathalye/imagenet-simple-labels/master/imagenet-simple-labels.json"
IMAGENET_CLASSES_FILE = "imagenet_labels.json"
IMAGE_SIZE=224
####################################################################

execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if not "python" in str(execution_ws):
    execution_ws = execution_ws / "python"

if not MODEL_NAME in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"
model_path = model_dir /  "{}.bin".format(MODEL_NAME)
imagenet_classes_path = model_dir / IMAGENET_CLASSES_FILE

input_image_path = execution_ws / "dog.jpg"
INPUT_IMAGE_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/imagenet_classifier/v1/dog.jpg"

###################################################################

googlenet=None

def format_float(num, max_zeros=6):
    if abs(num) >= 1e-6:
        return f"{num:.10f}".rstrip('0').rstrip('.')
    else:
        return f"{num:.{max_zeros+2}f}".rstrip('0').rstrip('.')
    
# GOOGLENET class which inherited from the class QNNContext.
class GoogleNet(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data] ##if don't have this line, affect accuracy
        output_data = super().Inference(input_datas)[0]
        return output_data

def normalize_image_transform(
    image_tensor: torch.Tensor,
    image_tensor_has_batch: bool = True,
    is_video: bool = False,
) -> torch.Tensor:
    """
    Normalizes according to standard torchvision constants.

    Due to issues with FX Graph tracing in AIMET, image_tensor_has_batch is a constant passed in,
    rather than determining the image rank using len(image_tensor.shape).

    There are many PyTorch models that expect input images normalized with
    these specific constants, so this utility can be re-used across many models.
    """
    shape = [-1, 1, 1]
    if image_tensor_has_batch:
        shape.insert(0, 1)
    if is_video:
        shape.append(1)
    mean = torch.Tensor([0.485, 0.456, 0.406]).reshape(*shape)
    std = torch.Tensor([[0.229, 0.224, 0.225]]).reshape(*shape)
    return (image_tensor - mean) / std

def preprocess_image(image, normalize: bool = False) -> torch.Tensor:
    preprocess = transforms.Compose(
    [
        transforms.Resize(IMAGE_SIZE),
        transforms.CenterCrop(IMAGE_SIZE),
        transforms.ToTensor(),
    ]
    )
    out_tensor: torch.Tensor = preprocess(image)  # type: ignore
    if normalize:
        out_tensor = normalize_image_transform(out_tensor,image_tensor_has_batch=False)

    return out_tensor.unsqueeze(0)


def model_download():
    ret = True

    if not os.path.exists(imagenet_classes_path):
        ret = install.download_url(IMAGENET_CLASSES_URL, imagenet_classes_path)

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(MODEL_ID, model_path, desc=desc, fail=fail)

    if not ret:
        exit()


def Init():
    global googlenet

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(str(qnn_dir), Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for GoogleNet objects.
    googlenet= GoogleNet("googlenet", str(model_path))



def Inference(input_image_path):
    # Read and preprocess the image.
    image = Image.open(input_image_path)
    image = preprocess_image(image).numpy()
    image = np.transpose(image, (0, 2, 3, 1))


    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

        # Run the inference.
    output_data = googlenet.Inference(image)

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()


    output = torch.from_numpy(output_data)  
    probabilities = torch.softmax(output, dim=0)
    result=post_process(probabilities, output)

    return result

def post_process(probabilities, output):
    # Read the categories
    with open(imagenet_classes_path, 'r', encoding='utf-8') as f:
        categories = json.load(f) 
    # Show top categories per image
    top5_prob, top5_catid = torch.topk(probabilities, 5)
    result = "Top 5 predictions for image:\n"
    print(result)

    for i in range(top5_prob.size(0)):
        cat_id = categories[top5_catid[i]]
        item_value = top5_prob[i].item()
        item_value = format_float(item_value)
        result += f'{cat_id} {item_value} \n'

        print(categories[top5_catid[i]], item_value)

    return result

def Release():
    global googlenet

    # Release the resources.
    del(googlenet)


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a single image path.")
    parser.add_argument('--image', help='Path to the image', default=None)
    args = parser.parse_args()

    main(args.image)







