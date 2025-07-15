# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import sys
import os
sys.path.append(".")
sys.path.append("python")
import utils.install as install
install.install_qai_appbuilder(install.DEFAULT_SDK_VER)
import cv2
import numpy as np
import numpy.typing as npt
import torch
import json
from PIL import Image
import argparse
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

####################################################################

MODEL_ID = "mnj1jvgdn"
MODEL_NAME = "face_attrib_net"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
IMAGE_SIZE = 128

OUT_NAMES = [
    "id_feature",
    "liveness_feature",
    "eye_closeness",
    "glasses",
    "mask",
    "sunglasses",
]

####################################################################

execution_ws = os.getcwd()
qnn_dir = execution_ws + "\\qai_libs"

if not "python" in execution_ws:
    execution_ws = execution_ws + "\\" + "python"

if not MODEL_NAME in execution_ws:
    execution_ws = execution_ws + "\\" + MODEL_NAME

model_dir = execution_ws + "\\models"
model_path = model_dir + "\\" + MODEL_NAME + ".bin"

output_path = execution_ws + "\\" + "build"

input_face_image_path = execution_ws + "\\input.bmp"
INPUT_FACE_IMAGE_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/face_attrib_net/v1/img_sample.bmp"
####################################################################

face_attrib_net = None

#FaceAttribNet class which inherited from the class QNNContext.
class FaceAttribNet(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)
        return output_data



def model_download():
    ret = True

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(MODEL_ID, model_path, desc=desc, fail=fail)

    if not ret:
        exit()

def Init():
    global face_attrib_net

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(qnn_dir, Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for FaceAttriNet objects.
    face_attrib_net = FaceAttribNet("face_attrib_net", model_path)


def preprocess_image(ori_image):
    img_array = np.array(ori_image)
    img_array = img_array.astype("float32") / 255  # image normalization
    img_array = img_array[np.newaxis, ...]
    # img_array = np.transpose(img_array,(0,3,1,2))   #
    return img_array

def post_process(raw_output):
    pred_res_list = [
        np.squeeze(out) for out in raw_output
    ]

    out_dict = {}
    for i in range(len(pred_res_list)):
        out_dict[OUT_NAMES[i]] = list(pred_res_list[i].astype(float))

    if not os.path.exists(output_path):
        os.mkdir(output_path)

    out_file = output_path+"\\output.json"
    with open(out_file, "w", encoding="utf-8") as wf:
        json.dump(out_dict, wf, ensure_ascii=False, indent=4)
    print(f"Model outputs are saved at: {out_file}")



def Inference(input_image_path):

    # Read and preprocess the image.
    ori_image = Image.open(input_image_path)

    input_image = preprocess_image(ori_image)

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    raw_output = face_attrib_net.Inference(input_image)

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()


    post_process(raw_output)


    
    
def Release():
    global face_attrib_net

    # Release the resources.
    del(face_attrib_net)

def main(input = None):

    if input is None:
        if not os.path.exists(input_face_image_path):
            ret = True
            ret = install.download_url(INPUT_FACE_IMAGE_PATH_URL, input_face_image_path)
        input = input_face_image_path

    Init()

    result = Inference(input)

    Release()

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a single image path.")
    parser.add_argument('--image', help='Path to the image', default=None)
    args = parser.parse_args()

    main(args.image)