# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import sys
import os
sys.path.append(".")
sys.path.append("..")
import utils.install as install
import cv2
import numpy as np
import torch
import torchvision.transforms as transforms
from PIL import Image
from PIL.Image import fromarray as ImageFromArray

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

####################################################################

MODEL_ID = "mmxeyyvyn"
MODEL_NAME = "inception_v3"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
IMAGENET_CLASSES_URL = "https://raw.githubusercontent.com/pytorch/hub/refs/heads/master/imagenet_classes.txt"
IMAGENET_CLASSES_FILE = "imagenet_classes.txt"
IMAGE_SIZE = 224

####################################################################

execution_ws = os.getcwd()
qnn_dir = execution_ws + "\\qai_libs"

if not MODEL_NAME in execution_ws:
    execution_ws = execution_ws + "\\" + MODEL_NAME

model_dir = execution_ws + "\\models"
madel_path = model_dir + "\\" + MODEL_NAME + ".bin"
imagenet_classes_path = model_dir + "\\" + IMAGENET_CLASSES_FILE

####################################################################

inceptionV3 = None

def preprocess_PIL_image(image: Image) -> torch.Tensor:
    preprocess = transforms.Compose([
        transforms.Resize(IMAGE_SIZE),
        transforms.CenterCrop(IMAGE_SIZE),
        transforms.ToTensor(),
    ])

    img = preprocess(image)
    img = img.unsqueeze(0)
    return img

def post_process(probabilities, output):
    # Read the categories
    with open(imagenet_classes_path, "r") as f:
        categories = [s.strip() for s in f.readlines()]
    # Show top categories per image
    top5_prob, top5_catid = torch.topk(probabilities, 5)
    print("Top 5 predictions for image:\n")
    for i in range(top5_prob.size(0)):
        print(categories[top5_catid[i]], top5_prob[i].item())

# InceptionV3 class which inherited from the class QNNContext.
class InceptionV3(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)[0]        
        return output_data

def model_download():
    ret = True

    if not os.path.exists(imagenet_classes_path):
        ret = install.download_url(IMAGENET_CLASSES_URL, imagenet_classes_path)

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(MODEL_ID, madel_path, desc=desc, fail=fail)

    if not ret:
        exit()

def Init():
    global inceptionV3

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(os.getcwd() + "\\qai_libs", Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for InceptionV3 objects.
    inceptionV3 = InceptionV3("inceptionV3", madel_path)

def Inference(input_image_path, output_image_path):
    # Read and preprocess the image.
    image = Image.open(input_image_path)
    image = preprocess_PIL_image(image).numpy()
    image = np.transpose(image, (0, 2, 3, 1))

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output_data = inceptionV3.Inference([image])

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()
    
    # show the Top 5 predictions for image
    output = torch.from_numpy(output_data)  
    probabilities = torch.softmax(output, dim=0)
    post_process(probabilities, output)
    

def Release():
    global inceptionV3

    # Release the resources.
    del(inceptionV3)


Init()

Inference(execution_ws + "\\input.jpg", "output.jpg")

Release()

