# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import os
import cv2
import numpy as np

import torch
import torchvision.transforms as transforms
from PIL import Image
from PIL.Image import fromarray as ImageFromArray

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

####################################################################

inceptionV3 = None

def preprocess_PIL_image(image: Image) -> torch.Tensor:
    preprocess = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
    ])

    img = preprocess(image)
    img = img.unsqueeze(0)
    return img

def post_process(probabilities, output):
    # Read the categories
    with open("imagenet_classes.txt", "r") as f:
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

def Init():
    global inceptionV3

    # Config AppBuilder environment.
    QNNConfig.Config(os.getcwd() + "\\qai_libs", Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for InceptionV3 objects.
    inceptionV3_model = "models\\inception_v3.bin"
    inceptionV3 = InceptionV3("inceptionV3", inceptionV3_model)

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

Inference("input.jpg", "output.jpg")

Release()

