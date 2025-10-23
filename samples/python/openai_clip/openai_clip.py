# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
sys.path.append(".")
sys.path.append("python")
import utils.install as install
from PIL import Image
import torch
import argparse
import numpy as np
import clip

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)
from pathlib import Path

####################################################################

MODEL_ID = "mmx0537em"
MODEL_NAME = "openai_clip"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
IMAGE_SIZE = 224
SEQ_LEN=77

PRETRAINED_WEIGHTS = "ViT-B/16"

####################################################################

execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if not "python" in str(execution_ws):
    execution_ws = execution_ws / "python"

if not MODEL_NAME in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"
model_path = model_dir /  "{}.bin".format(MODEL_NAME)

images_dir_path = execution_ws / "images"
images1_path = images_dir_path / "image1.jpg"
images2_path = images_dir_path / "image2.jpg"
images3_path = images_dir_path / "image3.jpg"
INPUT_IMAGE1_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/openai_clip/v1/image1.jpg"
INPUT_IMAGE2_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/openai_clip/v1/image2.jpg"
INPUT_IMAGE3_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/openai_clip/v1/image3.jpg"
####################################################################

openai_clip=None
text_tokenizer=None
image_preprocessor=None

# OpenAIClip class which inherited from the class QNNContext.
class OpenAIClip(QNNContext):
    def Inference(self,image,text):
        input_datas=[image, text]
        output_data = super().Inference(input_datas)[0]
        return output_data
    

def model_download():
    ret = True

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(MODEL_ID, model_path, desc=desc, fail=fail)

    if not os.path.exists(images_dir_path):
        ret = os.makedirs(images_dir_path)
        if not ret:
            ret = install.download_url(INPUT_IMAGE1_PATH_URL, images1_path)
            ret = install.download_url(INPUT_IMAGE2_PATH_URL, images2_path)
            ret = install.download_url(INPUT_IMAGE3_PATH_URL, images3_path)

    if not ret:
        exit()

def Init():
    global openai_clip,text_tokenizer,image_preprocessor

    model_download()

    device = "cpu"
    text_tokenizer = clip.tokenize
    net, image_preprocessor = clip.load(PRETRAINED_WEIGHTS, device=device)

    # Config AppBuilder environment.
    QNNConfig.Config(str(qnn_dir), Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for InceptionV3 objects.
    openai_clip = OpenAIClip("openai_clip", str(model_path))

def load_images_from_dir(images_dir):
    images,image_names = [],[]
    for image_name in os.listdir(images_dir):
        image_path = os.path.join(images_dir,image_name)
        image = Image.open(image_path)
        images.append(image)
        image_names.append(image_name)
    return images,image_names

def preprocess_image(image):
    img = image_preprocessor(image).unsqueeze(0)
    img = np.transpose(img.numpy(),(0,2,3,1))
    return img


def Inference(images_dir,text):
    # Read and preprocess the image.
    images,image_names = load_images_from_dir(images_dir)

    if not images :
        print(f"\nPlease put image files under dir: {images_dir_path} firstly!!!\n")
        exit()
    input_images =[preprocess_image(img) for img in images]

    #preprocess the text
    input_text= text_tokenizer(text).numpy()


    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    similarity_scores=[]
    for img in input_images:
        output_similarity = openai_clip.Inference(img,input_text)
        similarity_scores.append(output_similarity)
    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()
    

    # Display all the images and their score wrt to the text prompt provided.
    print(f"Searching images by prompt: {text}")
    for i in range(len(similarity_scores)):
        print(
            f"\t Image with name: {image_names[i]} has a similarity score={similarity_scores[i]}"
        )
    # Show image
    print("Displaying the most relevant image")
    max_idx = np.argmax(similarity_scores)
    images[max_idx].show()


def Release():
    global openai_clip

    # Release the resources.
    del(openai_clip)

def main(images_dir = None,text=None):
    Init()
    if images_dir is None:
        images_dir = execution_ws / "images"

    if text is None:
        text='camping under the stars'

    Inference(images_dir,text)
    Release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--images_dir', help='the directory of input images', default=None)
    parser.add_argument('--text',help='the prompt', default=None)
    args = parser.parse_args()

    main(args.images_dir,args.text)