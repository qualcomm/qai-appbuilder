# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import os
import numpy as np
import math
import torch
import torchvision.transforms as transforms

from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from torch.nn.functional import interpolate, pad
from torchvision import transforms
from typing import Callable, Dict, List, Tuple

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

unetsegmentation = None

def preprocess_PIL_image(image: Image) -> torch.Tensor:
    """Convert a PIL image into a pyTorch tensor with range [0, 1] and shape NCHW."""
    transform = transforms.Compose([transforms.PILToTensor()])  # bgr image
    img: torch.Tensor = transform(image)  # type: ignore
    img = img.float().unsqueeze(0) / 255.0  # int 0 - 255 to float 0.0 - 1.0
    return img

def torch_tensor_to_PIL_image(data: torch.Tensor) -> Image:
    """
    Convert a Torch tensor (dtype float32) with range [0, 1] and shape CHW into PIL image CHW
    """
    out = torch.clip(data, min=0.0, max=1.0)
    np_out = (out.permute(1, 2, 0).detach().numpy() * 255).astype(np.uint8)
    return ImageFromArray(np_out)

def resize_pad(image: torch.Tensor, dst_size: Tuple[int, int]):
    """
    Resize and pad image to be shape [..., dst_size[0], dst_size[1]]

    Parameters:
        image: (..., H, W)
            Image to reshape.

        dst_size: (height, width)
            Size to which the image should be reshaped.

    Returns:
        rescaled_padded_image: torch.Tensor (..., dst_size[0], dst_size[1])
        scale: scale factor between original image and dst_size image, (w, h)
        pad: pixels of padding added to the rescaled image: (left_padding, top_padding)

    Based on https://github.com/zmurez/MediaPipePyTorch/blob/master/blazebase.py
    """
    height, width = image.shape[-2:]
    dst_frame_height, dst_frame_width = dst_size

    h_ratio = dst_frame_height / height
    w_ratio = dst_frame_width / width
    scale = min(h_ratio, w_ratio)
    if h_ratio < w_ratio:
        scale = h_ratio
        new_height = dst_frame_height
        new_width = math.floor(width * scale)
    else:
        scale = w_ratio
        new_height = math.floor(height * scale)
        new_width = dst_frame_width

    new_height = math.floor(height * scale)
    new_width = math.floor(width * scale)
    pad_h = dst_frame_height - new_height
    pad_w = dst_frame_width - new_width

    pad_top = int(pad_h // 2)
    pad_bottom = int(pad_h // 2 + pad_h % 2)
    pad_left = int(pad_w // 2)
    pad_right = int(pad_w // 2 + pad_w % 2)

    rescaled_image = interpolate(
        image, size=[int(new_height), int(new_width)], mode="bilinear"
    )
    rescaled_padded_image = pad(
        rescaled_image, (pad_left, pad_right, pad_top, pad_bottom)
    )
    padding = (pad_left, pad_top)

    return rescaled_padded_image, scale, padding

def pil_resize_pad(
    image: Image, dst_size: Tuple[int, int]
) -> Tuple[Image, float, Tuple[int, int]]:
    torch_image = preprocess_PIL_image(image)
    torch_out_image, scale, padding = resize_pad(
        torch_image,
        dst_size,
    )
    pil_out_image = torch_tensor_to_PIL_image(torch_out_image[0])
    return (pil_out_image, scale, padding)

# UnetSegmentation class which inherited from the class QNNContext.
class UnetSegmentation(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data
        
def Init():
    global unetsegmentation

    # Config AppBuilder environment.
    QNNConfig.Config(os.getcwd() + "\\qnn", Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for UnetSegmentation objects.
    unetsegmentation_model = "models\\unet_segmentation.bin"
    unetsegmentation = UnetSegmentation("unetsegmentation", unetsegmentation_model)

def Inference(input_image_path, output_image_path): 
    # Read and preprocess the image&mask.
    orig_image = Image.open(input_image_path)
    image, _, _ = pil_resize_pad(orig_image, (640, 1280))
    img = preprocess_PIL_image(image).numpy()
    img = np.transpose(img, (0, 2, 3, 1))

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    out = unetsegmentation.Inference([img])
    
    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()
      
    # reshape the output
    out = out.reshape(1, 640, 1280, 2)
    output_tensor = torch.from_numpy(out)
    output_tensor = output_tensor.permute(0, 3, 1, 2)
    
    # get mask
    mask = output_tensor.argmax(dim=1)

    # show&save the result
    output_image = Image.fromarray(mask[0].bool().numpy())
    output_image.save(output_image_path)
    output_image.show()


def Release():
    global unetsegmentation

    # Release the resources.
    del(unetsegmentation)


Init()

Inference("input.jpg", "output.jpg")

Release()
