# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

from __future__ import annotations
import sys
import os
sys.path.append(".")
sys.path.append("..")
import utils.install as install
import numpy as np
import math
import torch
import torchvision.transforms as transforms
from typing import Callable, Dict, List, Tuple
from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from torch.nn.functional import interpolate, pad
from torchvision import transforms
from ultralytics.engine.results import Results
from ultralytics.models.fastsam import FastSAMPrompt
from ultralytics.models.fastsam.utils import bbox_iou
from ultralytics.utils import ops

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

####################################################################

MODEL_ID = "mn7x79pvq"
MODEL_NAME = "fastsam_x"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"

####################################################################

execution_ws = os.getcwd()
qnn_dir = execution_ws + "\\qai_libs"

if not MODEL_NAME in execution_ws:
    execution_ws = execution_ws + "\\" + MODEL_NAME

model_dir = execution_ws + "\\models"
madel_path = model_dir + "\\" + MODEL_NAME + ".bin"

####################################################################

fastsam = None

confidence: float = 0.4,
iou_threshold: float = 0.9,
retina_masks: bool = True,
model_image_input_shape: Tuple[int, int] = (640, 640)

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

def undo_resize_pad(
    image: torch.Tensor,
    orig_size_wh: Tuple[int, int],
    scale: float,
    padding: Tuple[int, int],
):
    """
    Undos the efffect of resize_pad. Instead of scale, the original size
    (in order width, height) is provided to prevent an off-by-one size.
    """
    width, height = orig_size_wh

    rescaled_image = interpolate(image, scale_factor=1 / scale, mode="bilinear")

    scaled_padding = [int(round(padding[0] / scale)), int(round(padding[1] / scale))]

    cropped_image = rescaled_image[
        ...,
        scaled_padding[1] : scaled_padding[1] + height,
        scaled_padding[0] : scaled_padding[0] + width,
    ]

    return cropped_image

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
    
def pil_undo_resize_pad(
    image: Image, orig_size_wh: Tuple[int, int], scale: float, padding: Tuple[int, int]
) -> Image:
    torch_image = preprocess_PIL_image(image)
    torch_out_image = undo_resize_pad(torch_image, orig_size_wh, scale, padding)
    pil_out_image = torch_tensor_to_PIL_image(torch_out_image[0])
    return pil_out_image
    
# FastSam_x class which inherited from the class QNNContext.
class FastSam(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)
        return output_data

def model_download():
    ret = True

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(MODEL_ID, madel_path, desc=desc, fail=fail)

    if not ret:
        exit()

def Init():
    global fastsam

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(os.getcwd() + "\\qai_libs", Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for FastSam_x objects.
    fastsam = FastSam("fastsam", madel_path)

def Inference(input_image_path, output_image_path): 
    global confidence, iou_threshold, retina_masks, model_image_input_shape

    # Read and preprocess the image.
    original_image = Image.open(input_image_path)
    resized_image, scale, padding = pil_resize_pad(original_image, (model_image_input_shape[0], model_image_input_shape[1]))
    
    Img = preprocess_PIL_image(resized_image)
    img = preprocess_PIL_image(resized_image).numpy()
    img = np.transpose(img, (0, 2, 3, 1))
    original_image = np.array(original_image)

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    image_path = ["out"]
    preds = fastsam.Inference([img])
    
    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()
    
    # Post Processing
    preds = [
        torch.tensor(preds[0]).reshape(1, 32, 160, 160),
        torch.tensor(preds[1]).reshape(1, 32, 8400),
        torch.tensor(preds[2]).reshape(1, 105, 80, 80),
        torch.tensor(preds[3]).reshape(1, 105, 40, 40),
        torch.tensor(preds[4]).reshape(1, 105, 20, 20),
        torch.tensor(preds[5]).reshape(1, 37, 8400)
    ]

    preds = tuple(
        (preds[5], tuple(([preds[2], preds[3], preds[4]], preds[1], preds[0])))
    )

    p = ops.non_max_suppression(
        preds[0],
        0.4,
        0.9,
        agnostic=False,
        max_det=100,
        nc=1,  # set to 1 class since SAM has no class predictions
        classes=None,
    )

    full_box = torch.zeros(p[0].shape[1], device=p[0].device)
    full_box[2], full_box[3], full_box[4], full_box[6:] = (
        Img.shape[3],
        Img.shape[2],
        1.0,
        1.0,
    )
    full_box = full_box.view(1, -1)
    critical_iou_index = bbox_iou(
        full_box[0][:4], p[0][:, :4], iou_thres=0.9, image_shape=Img.shape[2:]
    )
    if critical_iou_index.numel() != 0:
        full_box[0][4] = p[0][critical_iou_index][:, 4]
        full_box[0][6:] = p[0][critical_iou_index][:, 6:]
        p[0][critical_iou_index] = full_box

    results = []
    proto = (
        preds[1][-1] if len(preds[1]) == 3 else preds[1]
    )  # second output is len 3 if pt, but only 1 if exported
    for i, pred in enumerate(p):
        orig_img = original_image
        img_path = image_path[0][i]
        # No predictions, no masks
        if not len(pred):
            masks = None
        elif retina_masks:
            pred[:, :4] = ops.scale_boxes(
                Img.shape[2:], pred[:, :4], orig_img.shape
            )

            masks = ops.process_mask_native(
                proto[i], pred[:, 6:], pred[:, :4], orig_img.shape[:2]
            )  # HWC
        else:
            masks = ops.process_mask(
                proto[i], pred[:, 6:], pred[:, :4], Img.shape[2:], upsample=True
            )  # HWC
            pred[:, :4] = ops.scale_boxes(
                Img.shape[2:], pred[:, :4], orig_img.shape
            )
        results.append(
            Results(
                orig_img,
                path=img_path,
                names="fastsam",
                boxes=pred[:, :6],
                masks=masks,
            )
        )
    
    # Get segmented_result
    prompt_process = FastSAMPrompt(image_path[0], results, device="cpu")
    #segmented_result = prompt_process.everything_prompt()
    #segmented_result = prompt_process.text_prompt(text='the yellow dog')
    segmented_result = prompt_process.point_prompt(points=[[290, 433]], pointlabel=[1])
    #segmented_result = prompt_process.box_prompt([320, 80, 40, 40])
    prompt_process.plot(annotations=segmented_result, output="output")
    
    # Get Binary Mask Result
    binary_mask = segmented_result[0].masks.data.squeeze().cpu().numpy().astype(np.uint8)
    binary_mask = binary_mask * 255
    mask_image = Image.fromarray(binary_mask)

    #save and display the output_image
    mask_image.save(output_image_path)
    mask_image.show()


def Release():
    global fastsam

    # Release the resources.
    del(fastsam)


Init()

Inference(execution_ws + "\\input.jpg", execution_ws + "\\output.jpg")

Release()

