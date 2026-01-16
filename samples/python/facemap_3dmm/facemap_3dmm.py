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
from PIL import Image
import torch
from typing import Tuple
from skimage import io
import cv2
import argparse
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)
from pathlib import Path

####################################################################

MODEL_ID = "mqyy9zd9q"
HUB_ID_H="ox06ibpbkxb4pr0mcyfe7wqgx5pf5r0cm3rf3dzi"

MODEL_NAME = "facemap_3dmm"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
FACE_IMG_FBOX_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/facemap_3dmm/v1/face_img_fbox.txt"
MEANFACE_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/facemap_3dmm/v1/meanFace.npy"
SHAPEBASIS_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/facemap_3dmm/v1/shapeBasis.npy"
BLENDSHAPE_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/facemap_3dmm/v1/blendShape.npy"
####################################################################

execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if not "python" in str(execution_ws):
    execution_ws = execution_ws / "python"

if not MODEL_NAME in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"
model_path = model_dir /  "{}.bin".format(MODEL_NAME)

face_img_fbox_path = execution_ws / "face_img_fbox.txt"
meanFace_path = execution_ws / "meanFace.npy"
shapeBasis_path = execution_ws / "shapeBasis.npy"
blendShape_path = execution_ws / "blendShape.npy"

####################################################################

facemap_3dmm = None

# OpenPose class which inherited from the class QNNContext.
class Facemap_3dmm(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)
        return output_data

def model_download():
    ret = True

    if not os.path.exists(face_img_fbox_path):
        ret = install.download_url(FACE_IMG_FBOX_PATH_URL, face_img_fbox_path)

    if not os.path.exists(meanFace_path):
        ret = install.download_url(MEANFACE_PATH_URL, meanFace_path)

    if not os.path.exists(shapeBasis_path):
        ret = install.download_url(SHAPEBASIS_PATH_URL, shapeBasis_path)

    if not os.path.exists(blendShape_path):
        ret = install.download_url(BLENDSHAPE_PATH_URL, blendShape_path)
        
    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(MODEL_ID, model_path, desc=desc, fail=fail, hub_id=HUB_ID_H)

    if not ret:
        exit()

def Init():
    global facemap_3dmm

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(str(qnn_dir), Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for OpnPose objects.
    facemap_3dmm = Facemap_3dmm("facemap_3dmm", str(model_path))

def save_image(image: Image, base_dir: str, filename: str, desc: str):
    os.makedirs(base_dir, exist_ok=True)
    filename = os.path.join(base_dir, filename)
    image.save(filename)
    print(f"Saving {desc} to {filename}")
    image.show()

def Inference(input_image_path): 
    # Load image
    _image = io.imread(input_image_path)

    fbox = np.loadtxt(face_img_fbox_path)
    x0, x1, y0, y1 = (
        np.int32(fbox[0]),
        np.int32(fbox[1]),
        np.int32(fbox[2]),
        np.int32(fbox[3]),
    )
   
    face = torch.from_numpy(np.load(meanFace_path).reshape(3 * 68, 1))
    basis_id = torch.from_numpy(np.load(shapeBasis_path).reshape(3 * 68, 219))
    basis_exp = torch.from_numpy(np.load(blendShape_path).reshape(3 * 68, 39))
    
    vertex_num = 68
    alpha_id_size = 219
    alpha_exp_Size = 39

    height = y1 - y0 + 1
    width = x1 - x0 + 1

    CHW_fp32_torch_crop_image = torch.from_numpy(
        cv2.resize(
            _image[y0 : y1 + 1, x0 : x1 + 1],
            (128, 128),
            interpolation=cv2.INTER_LINEAR,
        )
    ).float()
    
    image = CHW_fp32_torch_crop_image.permute(2, 0, 1).view(1, 3, 128, 128).detach().cpu().numpy()
    
    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output = facemap_3dmm.Inference(image)

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()

    # postprocess the result
    _output = torch.from_numpy(output[0])  # shape (1, 265)
    alpha_id  = _output[0, 0:219]
    alpha_exp = _output[0, 219:258]
    pitch     = _output[0, 258]
    yaw       = _output[0, 259]
    roll      = _output[0, 260]
    tX        = _output[0, 261]
    tY        = _output[0, 262]
    f         = _output[0, 263]

    # De-normalized to original range from [-1, 1]
    alpha_id = alpha_id * 3
    alpha_exp = alpha_exp * 0.5 + 0.5
    pitch = pitch * np.pi / 2
    yaw = yaw * np.pi / 2
    roll = roll * np.pi / 2
    tX = tX * 60
    tY = tY * 60
    tZ = 500
    f = f * 150 + 450

    p_matrix = torch.tensor(
        [
            [1, 0, 0],
            [0, torch.cos(-torch.tensor(np.pi)), -torch.sin(-torch.tensor(np.pi))],
            [0, torch.sin(-torch.tensor(np.pi)), torch.cos(-torch.tensor(np.pi))],
        ]
    )

    # Create a rotation matrix from pitch, yaw, roll
    roll_matrix = torch.tensor(
        [
            [torch.cos(-roll), -torch.sin(-roll), 0],
            [torch.sin(-roll), torch.cos(-roll), 0],
            [0, 0, 1],
        ]
    )

    yaw_matrix = torch.tensor(
        [
            [torch.cos(-yaw), 0, torch.sin(-yaw)],
            [0, 1, 0],
            [-torch.sin(-yaw), 0, torch.cos(-yaw)],
        ]
    )

    pitch_matrix = torch.tensor(
        [
            [1, 0, 0],
            [0, torch.cos(-pitch), -torch.sin(-pitch)],
            [0, torch.sin(-pitch), torch.cos(-pitch)],
        ]
    )

    r_matrix = torch.mm(
        yaw_matrix, torch.mm(pitch_matrix, torch.mm(p_matrix, roll_matrix))
    )

    # Reconstruct face
    vertices = torch.mm(
        (
            face
            + torch.mm(basis_id, alpha_id.view(219, 1))
            + torch.mm(basis_exp, alpha_exp.view(39, 1))
        ).view([vertex_num, 3]),
        r_matrix.transpose(0, 1),
    )

    # Apply translation
    vertices[:, 0] += tX
    vertices[:, 1] += tY
    vertices[:, 2] += tZ

    # Project landmark vertices to 2D
    f = torch.tensor([f, f]).float()
    landmark = vertices[:, 0:2] * f / tZ + 128 / 2

    landmark[:, 0] = landmark[:, 0] * width / 128 + x0
    landmark[:, 1] = landmark[:, 1] * height / 128 + y0

    # Draw landmark
    output_image = cv2.cvtColor(_image, cv2.COLOR_RGB2BGR)
    for n in range(landmark.shape[0]):
        output_image = cv2.circle(
            output_image,
            (int(landmark[n, 0]), int(landmark[n, 1])),
            2,
            (0, 0, 255),
            -1,
        )

    np.savetxt(
        execution_ws / "demo_output_lmk.txt",
        landmark.detach().numpy(),
    )
    
    save_image(
        Image.fromarray(cv2.cvtColor(output_image, cv2.COLOR_BGR2RGB)),
        execution_ws,
        "output.jpg",
        "image"
    )

def Release():
    global facemap_3dmm

    # Release the resources.
    del(facemap_3dmm)


def main(input=None):
    if input is None:
        input = execution_ws / "input.jpg"

    Init()
    Inference(input)

    Release()


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Process a single image path.")
    parser.add_argument('--image', help='Path to the image', default=None)
    args = parser.parse_args()
    
    main(args.image)


