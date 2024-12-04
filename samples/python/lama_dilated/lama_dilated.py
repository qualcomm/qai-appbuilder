# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import os
import numpy as np
import torch
import torchvision.transforms as transforms

from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from torch.nn.functional import interpolate, pad
from torchvision import transforms
from typing import Callable, Dict, List, Tuple

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

image_size = 512
lamadilated = None
image_buffer = None


def preprocess_PIL_image(image: Image) -> torch.Tensor:
    """Convert a PIL image into a pyTorch tensor with range [0, 1] and shape NCHW."""
    transform = transforms.Compose([transforms.Resize(image_size),      # bgr image
                                    transforms.CenterCrop(image_size),
                                    transforms.PILToTensor()])
    img: torch.Tensor = transform(image)  # type: ignore
    img = img.float().unsqueeze(0) / 255.0  # int 0 - 255 to float 0.0 - 1.0
    return img

def torch_tensor_to_PIL_image(data: torch.Tensor) -> Image:
    """
    Convert a Torch tensor (dtype float32) with range [0, 1] and shape CHW into PIL image CHW
    """
    out = torch.clip(data, min=0.0, max=1.0)
    np_out = (out.detach().numpy() * 255).astype(np.uint8)
    return ImageFromArray(np_out)
   
def preprocess_inputs(
    pixel_values_or_image: Image,
    mask_pixel_values_or_image: Image,
) -> Dict[str, torch.Tensor]:

    NCHW_fp32_torch_frames = preprocess_PIL_image(pixel_values_or_image)
    NCHW_fp32_torch_masks = preprocess_PIL_image(mask_pixel_values_or_image)
    
    # The number of input images should equal the number of input masks.
    if NCHW_fp32_torch_masks.shape[0] != 1:
        NCHW_fp32_torch_masks = NCHW_fp32_torch_masks.tile(
            (NCHW_fp32_torch_frames.shape[0], 1, 1, 1)
        )
  
    # Mask input image
    image_masked = (
        NCHW_fp32_torch_frames * (1 - NCHW_fp32_torch_masks) + NCHW_fp32_torch_masks
    )
    
    return {"image": image_masked, "mask": NCHW_fp32_torch_masks}

# LamaDilated class which inherited from the class QNNContext.
class LamaDilated(QNNContext):
    def Inference(self, input_data, input_mask):
        input_datas=[input_data, input_mask]
        output_data = super().Inference(input_datas)[0]
        return output_data
        
def Init():
    global lamadilated

    # Config AppBuilder environment.
    QNNConfig.Config(os.getcwd() + "\\qnn", Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for LamaDilated objects.
    lamadilated_model = "models\\lama_dilated.bin"
    lamadilated = LamaDilated("lamadilated", lamadilated_model)

def Inference(input_image_path, input_mask_path, output_image_path):
    global image_buffer

    # Read and preprocess the image&mask.
    image = Image.open(input_image_path)
    mask = Image.open(input_mask_path)   
    inputs = preprocess_inputs(image, mask)
    image_masked, mask_torch = inputs["image"], inputs["mask"]         
    image_masked = image_masked.numpy()
    mask_torch = mask_torch.numpy()
     
    image_masked = np.transpose(image_masked, (0, 2, 3, 1)) 
    mask_torch = np.transpose(mask_torch, (0, 2, 3, 1)) 
             
    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output_image = lamadilated.Inference([image_masked], [mask_torch])
    
    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()
    
    # show%save the result
    output_image = torch.from_numpy(output_image)    
    output_image = output_image.reshape(image_size, image_size, 3)  
    output_image = torch.unsqueeze(output_image, 0)      
    output_image = [torch_tensor_to_PIL_image(img) for img in output_image]
    image_buffer = output_image[0]
    image_buffer.show()  
    image_buffer.save(output_image_path)

def Release():
    global lamadilated

    # Release the resources.
    del(lamadilated)


Init()

Inference("input.jpg", "mask.jpg", "output.jpg")

Release()
