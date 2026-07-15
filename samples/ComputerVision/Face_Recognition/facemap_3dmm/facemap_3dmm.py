# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import sys
import os
import time
import zipfile
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(".")
sys.path.append(os.path.join(_SCRIPT_DIR, "..", "..", "..", "common"))

import install
import numpy as np
from PIL import Image
import torch
from skimage import io
import cv2
import argparse
from pathlib import Path
import platform

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)
from _face_recognition import (
    FaceRecognitionQNNContext,
    init_htp_model,
    download_model,
    run_inference_with_perf_profile,
    save_image,
)

# Try to import OnnxRuntimeContext (available in newer qai_appbuilder versions)
try:
    from qai_appbuilder import OnnxRuntimeContext
    _ONNX_RUNTIME_AVAILABLE = True
except ImportError:
    _ONNX_RUNTIME_AVAILABLE = False

####################################################################

MODEL_ID   = "mqyy9zd9q"
HUB_ID_H   = "ox06ibpbkxb4pr0mcyfe7wqgx5pf5r0cm3rf3dzi"
MODEL_NAME = "facemap_3dmm"
MODEL_HELP_URL = "https://github.com/qualcomm/qai-appbuilder/blob/main/samples/computerVision/Face_Recognition/facemap_3dmm/README.md"

FACE_IMG_FBOX_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/facemap_3dmm/v1/face_img_fbox.txt"
MEANFACE_PATH_URL      = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/facemap_3dmm/v1/meanFace.npy"
SHAPEBASIS_PATH_URL    = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/facemap_3dmm/v1/shapeBasis.npy"
BLENDSHAPE_PATH_URL    = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/facemap_3dmm/v1/blendShape.npy"

# ONNX float model gives more accurate results than the quantized QNN model
ONNX_ZIP_URL      = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/facemap_3dmm/releases/v0.47.0/facemap_3dmm-onnx-float.zip"
ONNX_ZIP_FILENAME = "facemap_3dmm-onnx-float.zip"
ONNX_MODEL_SUBPATH = "facemap_3dmm-onnx-float/facemap_3dmm.onnx"

####################################################################

execution_ws = Path(os.path.dirname(os.path.abspath(__file__)))
model_dir    = execution_ws / "models"
model_path   = model_dir / "{}.bin".format(MODEL_NAME)

face_img_fbox_path = execution_ws / "face_img_fbox.txt"
meanFace_path      = execution_ws / "meanFace.npy"
shapeBasis_path    = execution_ws / "shapeBasis.npy"
blendShape_path    = execution_ws / "blendShape.npy"

####################################################################

SOC_ID = None
cleaned_argv = []
i = 0
while i < len(sys.argv):
    if sys.argv[i] == '--chipset':
        SOC_ID = sys.argv[i + 1]
        i += 2
    else:
        cleaned_argv.append(sys.argv[i])
        i += 1

sys.argv = cleaned_argv
print(f"SOC_ID: {SOC_ID}")

def _detect_platform():
    system  = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "wos" if machine in ("aarch64", "arm64") else "x86_win"
    if system == "linux":
        if machine in ("aarch64", "arm64"):
            return "arm64_linux"
        if machine in ("x86_64", "amd64"):
            return "x86_linux"
    return "unknown"

PLATFORM = _detect_platform()
print(f"[INFO] Detected platform: {PLATFORM}")

####################################################################

facemap_3dmm = None
_using_onnx  = False


class Facemap3dmmQNN(FaceRecognitionQNNContext):
    """QNN wrapper for facemap_3dmm."""
    pass


def _download_onnx_model():
    """Download and extract the ONNX float model. Returns path to .onnx or None."""
    onnx_model_path = model_dir / ONNX_MODEL_SUBPATH
    if onnx_model_path.exists():
        return onnx_model_path

    model_dir.mkdir(parents=True, exist_ok=True)
    onnx_zip_path = model_dir / ONNX_ZIP_FILENAME
    if not onnx_zip_path.exists():
        print(f"[INFO] Downloading {ONNX_ZIP_FILENAME} (ONNX float model for better accuracy)...")
        install.download_url(ONNX_ZIP_URL, onnx_zip_path)

    if onnx_zip_path.exists():
        print(f"[INFO] Extracting {ONNX_ZIP_FILENAME}...")
        try:
            with zipfile.ZipFile(onnx_zip_path, "r") as zf:
                zf.extractall(path=model_dir)
            onnx_zip_path.unlink(missing_ok=True)
        except Exception as e:
            print(f"[WARN] Extraction failed: {e}")
            return None

    return onnx_model_path if onnx_model_path.exists() else None


def model_download():
    """Download all required assets and model."""
    # Download extra resources
    for url, path in [
        (FACE_IMG_FBOX_PATH_URL, face_img_fbox_path),
        (MEANFACE_PATH_URL,      meanFace_path),
        (SHAPEBASIS_PATH_URL,    shapeBasis_path),
        (BLENDSHAPE_PATH_URL,    blendShape_path),
    ]:
        if not os.path.exists(path):
            install.download_url(url, path)

    # Try ONNX model first (more accurate)
    if _ONNX_RUNTIME_AVAILABLE:
        onnx_path = _download_onnx_model()
        if onnx_path and onnx_path.exists():
            return  # ONNX model ready, skip QNN download

    # Fall back to QNN model
    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    if PLATFORM in ("wos", "x86_win"):
        ret = install.download_qai_hubmodel(SOC_ID, MODEL_NAME, model_path, desc=desc, fail=fail, hub_id=HUB_ID_H)
    else:
        ret = install.download_qai_hubmodel(SOC_ID, MODEL_NAME, model_path, desc=desc, fail=fail)
    if not ret:
        exit()


def Init():
    global facemap_3dmm, _using_onnx

    model_download()

    # Prefer ONNX float model for better accuracy
    if _ONNX_RUNTIME_AVAILABLE:
        onnx_path = model_dir / ONNX_MODEL_SUBPATH
        if onnx_path.exists():
            print(f"[INFO] Using ONNX float model for {MODEL_NAME} (better accuracy)")
            facemap_3dmm = OnnxRuntimeContext(MODEL_NAME, str(onnx_path), use_cpu=False)
            _using_onnx = True
            return

    # Fall back to QNN model
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
    facemap_3dmm = Facemap3dmmQNN(MODEL_NAME, str(model_path))
    _using_onnx = False
    print(f"[INFO] Using QNN quantized model for {MODEL_NAME}")


def Inference(input_image_path):
    global facemap_3dmm, _using_onnx

    # Load image
    _image = io.imread(input_image_path)

    # Load face bounding box
    fbox = np.loadtxt(face_img_fbox_path)
    x0, x1, y0, y1 = (
        np.int32(fbox[0]),
        np.int32(fbox[1]),
        np.int32(fbox[2]),
        np.int32(fbox[3]),
    )

    # Load 3DMM basis
    face       = torch.from_numpy(np.load(meanFace_path).reshape(3 * 68, 1))
    basis_id   = torch.from_numpy(np.load(shapeBasis_path).reshape(3 * 68, 219))
    basis_exp  = torch.from_numpy(np.load(blendShape_path).reshape(3 * 68, 39))

    vertex_num = 68
    height = y1 - y0 + 1
    width  = x1 - x0 + 1

    crop = cv2.resize(
        _image[y0 : y1 + 1, x0 : x1 + 1],
        (128, 128),
        interpolation=cv2.INTER_LINEAR,
    )

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    if _using_onnx:
        # ONNX float model: normalize to [0, 1], NCHW layout
        inp = (crop.astype(np.float32) / 255.0)
        inp = np.transpose(inp, (2, 0, 1))[None, ...]  # HWC -> NCHW
        start_time = time.perf_counter()
        outputs = facemap_3dmm.Inference([inp])
        elapsed_time = time.perf_counter() - start_time
        print(f"[INFO] ONNX model inference time: {elapsed_time*1000:.2f} ms")
        out0 = np.asarray(outputs[0])
    else:
        # QNN model: raw [0, 255] float, NCHW layout
        image = torch.from_numpy(crop).float().permute(2, 0, 1).view(1, 3, 128, 128).detach().cpu().numpy()
        output = facemap_3dmm.Inference(image)
        out0 = np.asarray(output[0])

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()

    # Post-process model output
    if out0.ndim == 1:
        out0 = out0[None, :]
    _output = torch.from_numpy(out0.reshape(1, -1))

    alpha_id  = _output[0, 0:219]
    alpha_exp = _output[0, 219:258]
    pitch     = _output[0, 258]
    yaw       = _output[0, 259]
    roll      = _output[0, 260]
    tX        = _output[0, 261]
    tY        = _output[0, 262]
    f         = _output[0, 263]

    # De-normalize from [-1, 1]
    alpha_id  = alpha_id * 3
    alpha_exp = alpha_exp * 0.5 + 0.5
    pitch = pitch * np.pi / 2
    yaw   = yaw   * np.pi / 2
    roll  = roll  * np.pi / 2
    tX = tX * 60
    tY = tY * 60
    tZ = 500
    f  = f * 150 + 450

    p_matrix = torch.tensor(
        [
            [1, 0, 0],
            [0, torch.cos(-torch.tensor(np.pi)), -torch.sin(-torch.tensor(np.pi))],
            [0, torch.sin(-torch.tensor(np.pi)),  torch.cos(-torch.tensor(np.pi))],
        ]
    )

    roll_matrix = torch.tensor(
        [
            [torch.cos(-roll), -torch.sin(-roll), 0],
            [torch.sin(-roll),  torch.cos(-roll), 0],
            [0, 0, 1],
        ]
    )

    yaw_matrix = torch.tensor(
        [
            [ torch.cos(-yaw), 0, torch.sin(-yaw)],
            [0, 1, 0],
            [-torch.sin(-yaw), 0, torch.cos(-yaw)],
        ]
    )

    pitch_matrix = torch.tensor(
        [
            [1, 0, 0],
            [0,  torch.cos(-pitch), -torch.sin(-pitch)],
            [0,  torch.sin(-pitch),  torch.cos(-pitch)],
        ]
    )

    r_matrix = torch.mm(
        yaw_matrix, torch.mm(pitch_matrix, torch.mm(p_matrix, roll_matrix))
    )

    # Reconstruct face vertices
    vertices = torch.mm(
        (
            face
            + torch.mm(basis_id,  alpha_id.view(219, 1))
            + torch.mm(basis_exp, alpha_exp.view(39, 1))
        ).view([vertex_num, 3]),
        r_matrix.transpose(0, 1),
    )

    # Apply translation
    vertices[:, 0] += tX
    vertices[:, 1] += tY
    vertices[:, 2] += tZ

    # Project landmark vertices to 2D
    f_tensor = torch.tensor([f, f]).float()
    landmark = vertices[:, 0:2] * f_tensor / tZ + 128 / 2

    landmark[:, 0] = landmark[:, 0] * width  / 128 + x0
    landmark[:, 1] = landmark[:, 1] * height / 128 + y0

    # Draw landmarks
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
    if facemap_3dmm is not None:
        del facemap_3dmm


def main(input=None):
    if input is None:
        input = execution_ws / "input.jpg"

    Init()
    Inference(input)
    Release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="3D face mapping using FaceMap 3DMM.")
    parser.add_argument('--image', help='Path to the image', default=None)
    args = parser.parse_args()

    main(args.image)
