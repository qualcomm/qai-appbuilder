# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
_image_editing.py — Shared utilities for image inpainting / editing models.

Used by:
    ComputerVision/Image_Editing/aotgan/aotgan.py
    ComputerVision/Image_Editing/lama_dilated/lama_dilated.py
"""

import numpy as np
import torch
import torchvision.transforms as transforms

from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from image_processing import preprocess_inputs
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

import install

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

IMAGE_SIZE = 512   # Both aotgan and lama_dilated use 512×512 input

# ─────────────────────────────────────────────────────────────────────────────
# Base QNNContext class for image editing models
# ─────────────────────────────────────────────────────────────────────────────

class ImageEditingQNNContext(QNNContext):
    """Base QNNContext for image inpainting models.

    Accepts two inputs: masked image [1,H,W,3] and mask [1,H,W,1],
    both in NHWC float32 format.
    Returns the inpainted output as a flat float32 array.
    """
    def Inference(self, input_data, input_mask):
        input_datas = [input_data, input_mask]
        output_data = super().Inference(input_datas)[0]
        return output_data


# ─────────────────────────────────────────────────────────────────────────────
# Model download helper
# ─────────────────────────────────────────────────────────────────────────────

def download_model(soc_id, model_name, model_path, model_help_url):
    """Download the QNN model binary via QAI Hub.

    Parameters
    ----------
    soc_id       : SoC ID string (or None for auto-detect)
    model_name   : Hub model name (e.g. "aotgan")
    model_path   : Destination path for the .bin file
    model_help_url : URL shown in the failure message

    Returns
    -------
    True on success, exits on failure.
    """
    desc = f"Downloading {model_name} model... "
    fail = (
        f"\nFailed to download {model_name} model. "
        f"Please prepare the model according to the steps in below link:\n{model_help_url}"
    )
    ret = install.download_qai_hubmodel(soc_id, model_name, model_path, desc=desc, fail=fail)
    if not ret:
        exit()
    return True


# ─────────────────────────────────────────────────────────────────────────────
# QNN configuration helper
# ─────────────────────────────────────────────────────────────────────────────

def init_htp_model(model_path, model_class, model_name):
    """Configure QNN HTP runtime and instantiate the model.

    Parameters
    ----------
    model_path  : Path to the .bin model file
    model_class : A subclass of ImageEditingQNNContext
    model_name  : Name string passed to QNNContext constructor

    Returns
    -------
    Instantiated model object.
    """
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
    return model_class(model_name, str(model_path))


# ─────────────────────────────────────────────────────────────────────────────
# Pre-processing
# ─────────────────────────────────────────────────────────────────────────────

def preprocess_for_inpainting(image_path, mask_path):
    """Load image + mask and prepare NHWC float32 arrays for QNN inference.

    Parameters
    ----------
    image_path : Path to the input image (any size; will be center-cropped to 512×512)
    mask_path  : Path to the binary mask  (same size handling)

    Returns
    -------
    image_nhwc : np.ndarray  shape [1, 512, 512, 3]  float32
    mask_nhwc  : np.ndarray  shape [1, 512, 512, 1]  float32
    orig_image : PIL.Image   the original opened image (for display)
    """
    image = Image.open(image_path)
    mask  = Image.open(mask_path)

    inputs = preprocess_inputs(image, mask)
    image_masked = inputs["image"].numpy()   # NCHW float32
    mask_torch   = inputs["mask"].numpy()    # NCHW float32

    image_nhwc = np.transpose(image_masked, (0, 2, 3, 1))  # → NHWC
    mask_nhwc  = np.transpose(mask_torch,   (0, 2, 3, 1))  # → NHWC

    return image_nhwc, mask_nhwc, image


# ─────────────────────────────────────────────────────────────────────────────
# Post-processing
# ─────────────────────────────────────────────────────────────────────────────

def postprocess_inpainted_output(output_data, image_size=IMAGE_SIZE):
    """Convert the flat model output to a PIL Image.

    Parameters
    ----------
    output_data : np.ndarray  flat float32 array from QNN
    image_size  : int         spatial size (default 512)

    Returns
    -------
    PIL.Image  the inpainted result
    """
    output = torch.from_numpy(output_data)
    output = output.reshape(image_size, image_size, 3)
    output = torch.unsqueeze(output, 0)
    pil_images = [torch_tensor_to_PIL_image(img) for img in output]
    return pil_images[0]


# ─────────────────────────────────────────────────────────────────────────────
# Performance-profile wrapper
# ─────────────────────────────────────────────────────────────────────────────

def run_inference_with_perf_profile(model, image_nhwc, mask_nhwc):
    """Run model inference under BURST performance profile.

    Parameters
    ----------
    model      : ImageEditingQNNContext instance
    image_nhwc : np.ndarray  [1, H, W, 3] float32
    mask_nhwc  : np.ndarray  [1, H, W, 1] float32

    Returns
    -------
    np.ndarray  flat float32 output from the model
    """
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    output = model.Inference(image_nhwc, mask_nhwc)
    PerfProfile.RelPerfProfileGlobal()
    return output


# ─────────────────────────────────────────────────────────────────────────────
# Image I/O helpers
# ─────────────────────────────────────────────────────────────────────────────

def save_image(pil_image, output_path, show=True, show_original=None):
    """Save a PIL image to disk and optionally display it.

    Parameters
    ----------
    pil_image     : PIL.Image  the image to save
    output_path   : str or Path  destination file path
    show          : bool  whether to call image.show()
    show_original : PIL.Image or None  if provided, also show the original
    """
    pil_image.save(str(output_path))
    if show:
        pil_image.show()
    if show_original is not None:
        show_original.show()


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers (also exported for direct use)
# ─────────────────────────────────────────────────────────────────────────────

def preprocess_PIL_image(image: Image.Image, image_size: int = IMAGE_SIZE) -> torch.Tensor:
    """Convert a PIL image into a PyTorch tensor [0, 1] with shape NCHW.

    Parameters
    ----------
    image      : PIL.Image
    image_size : int  target spatial size (resize + center-crop)

    Returns
    -------
    torch.Tensor  shape [1, C, H, W], dtype float32, values in [0, 1]
    """
    transform = transforms.Compose([
        transforms.Resize(image_size),
        transforms.CenterCrop(image_size),
        transforms.PILToTensor(),
    ])
    img: torch.Tensor = transform(image)
    img = img.float().unsqueeze(0) / 255.0
    return img


def torch_tensor_to_PIL_image(data: torch.Tensor) -> Image.Image:
    """Convert a float32 CHW tensor [0, 1] to a PIL Image.

    Parameters
    ----------
    data : torch.Tensor  shape [C, H, W], dtype float32, values in [0, 1]

    Returns
    -------
    PIL.Image
    """
    out = torch.clip(data, min=0.0, max=1.0)
    np_out = (out.detach().numpy() * 255).astype(np.uint8)
    return ImageFromArray(np_out)
