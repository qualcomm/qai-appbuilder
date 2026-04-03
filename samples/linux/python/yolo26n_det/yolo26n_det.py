# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import sys
import os
sys.path.append(".")
sys.path.append("python")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import utils.install as install
import cv2
import numpy as np
import torch
import torchvision.transforms as transforms
from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from torch.nn.functional import interpolate, pad
from torchvision.ops import nms
from typing import List, Tuple, Optional, Union, Callable
import argparse

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

####################################################################

IMAGE_SIZE = 640

####################################################################

execution_ws=os.path.dirname(os.path.abspath(__file__))
print(f"Current file directory: {execution_ws}")
qnn_sdk_root = os.environ.get("QNN_SDK_ROOT")
if not qnn_sdk_root:
    print("Error: QNN_SDK_ROOT environment variable is not set.")
    sys.exit(1)

qnn_dir = os.path.join(qnn_sdk_root, "lib/aarch64-oe-linux-gcc11.2")

model_path = "<path to your yolo26 series qnn model>"

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

yolo26n = None

# define class type
class_map = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    4: "airplane",
    5: "bus",
    6: "train",
    7: "truck",
    8: "boat",
    9: "traffic light",
    10: "fire hydrant",
    11: "stop sign",
    12: "parking meter",
    13: "bench",
    14: "bird",
    15: "cat",
    16: "dog",
    17: "horse",
    18: "sheep",
    19: "cow",
    20: "elephant",
    21: "bear",
    22: "zebra",
    23: "giraffe",
    24: "backpack",
    25: "umbrella",
    26: "handbag",
    27: "tie",
    28: "suitcase",
    29: "frisbee",
    30: "skis",
    31: "snowboard",
    32: "sports ball",
    33: "kite",
    34: "baseball bat",
    35: "baseball glove",
    36: "skateboard",
    37: "surfboard",
    38: "tennis racket",
    39: "bottle",
    40: "wine glass",
    41: "cup",
    42: "fork",
    43: "knife",
    44: "spoon",
    45: "bowl",
    46: "banana",
    47: "apple",
    48: "sandwich",
    49: "orange",
    50: "broccoli",
    51: "carrot",
    52: "hot dog",
    53: "pizza",
    54: "donut",
    55: "cake",
    56: "chair",
    57: "couch",
    58: "potted plant",
    59: "bed",
    60: "dining table",
    61: "toilet",
    62: "tv",
    63: "laptop",
    64: "mouse",
    65: "remote",
    66: "keyboard",
    67: "cell phone",
    68: "microwave",
    69: "oven",
    70: "toaster",
    71: "sink",
    72: "refrigerator",
    73: "book",
    74: "clock",
    75: "vase",
    76: "scissors",
    77: "teddy bear",
    78: "hair drier",
    79: "toothbrush"
}

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

def draw_box_from_xyxy(
    frame: np.ndarray,
    top_left: np.ndarray | torch.Tensor | Tuple[int, int],
    bottom_right: np.ndarray | torch.Tensor | Tuple[int, int],
    color: Tuple[int, int, int] = (0, 0, 0),
    size: int = 3,
    text: Optional[str] = None,
):
    """
    Draw a box using the provided top left / bottom right points to compute the box.

    Parameters:
        frame: np.ndarray
            np array (H W C x uint8, BGR)

        box: np.ndarray | torch.Tensor
            array (4), where layout is
                [xc, yc, h, w]

        color: Tuple[int, int, int]
            Color of drawn points and connection lines (RGB)

        size: int
            Size of drawn points and connection lines BGR channel layout

        text: None | str
            Overlay text at the top of the box.

    Returns:
        None; modifies frame in place.
    """
    original_height, original_width = frame.shape[:2]
    scale_x = original_width / IMAGE_SIZE
    scale_y = original_height / IMAGE_SIZE
    if not isinstance(top_left, tuple):
        top_left = (int(top_left[0].item()*scale_x), int(top_left[1].item()*scale_y))
    if not isinstance(bottom_right, tuple):
        bottom_right = (int(bottom_right[0].item()*scale_x), int(bottom_right[1].item()*scale_y))
    
    cv2.rectangle(frame, top_left, bottom_right, color, size)
    if text is not None:
        cv2.putText(
            frame,
            text,
            (top_left[0], top_left[1] - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            size,
        )

# Yolo26 class which inherited from the class QNNContext.
class Yolo26(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)    
        return output_data
    

def Init():
    global yolo26

    # Config AppBuilder environment.
    QNNConfig.Config(qnn_dir, Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for Yolo26 objects.
    yolo26 = Yolo26("yolo26", model_path)
    
    

def Inference(input_image_path, output_image_path, show_image=True, confidence: float = 0.60):
    global image_buffer

    # Read and preprocess the image.
    image = Image.open(input_image_path)
    image = image.resize((IMAGE_SIZE, IMAGE_SIZE))
    outputImg = Image.open(input_image_path)
    image = preprocess_PIL_image(image) # transfer raw image to torch tensor format
    # image  = image.permute(0, 2, 3, 1)
    image = image.numpy()

    output_image = np.array(outputImg.convert("RGB"))  # transfer to numpy array
    
    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    model_output = yolo26.Inference(image)
    
    pred_boxes = torch.tensor(model_output[0][:, :, :4])
    pred_scores = torch.tensor(model_output[0][:, :, 4])
    pred_class_idx = torch.tensor(model_output[0][:, :, 5])

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()

    # Add boxes to each batch
    for batch_idx in range(len(pred_boxes)):
        pred_boxes_batch = pred_boxes[batch_idx]
        pred_scores_batch = pred_scores[batch_idx]
        pred_class_idx_batch = pred_class_idx[batch_idx]

        keep_mask = pred_scores_batch >= confidence
        if not torch.any(keep_mask):
            continue

        pred_boxes_batch = pred_boxes_batch[keep_mask]
        pred_scores_batch = pred_scores_batch[keep_mask]
        pred_class_idx_batch = pred_class_idx_batch[keep_mask]

        for box, score, class_idx in zip(pred_boxes_batch, pred_scores_batch, pred_class_idx_batch):
            class_idx_item = round(class_idx.item()) 
            class_name = class_map.get(class_idx_item, "Unknown")
            draw_box_from_xyxy(
                output_image,
                box[0:2].int(),
                box[2:4].int(),
                color=(0, 255, 0),
                size=2,
                text=f'{score.item():.2f} {class_name}'
            )

    #save and display the output_image
    output_image = Image.fromarray(output_image)
    output_image.save(output_image_path)

    if show_image:
        output_image.show()

def Release():
    global yolo26

    # Release the resources.
    del(yolo26)


def main(input_image_path=None, output_image_path=None, show_image=False, confidence: float = 0.80):

    if input_image_path is None:
        input_image_path = os.path.join(execution_ws, "input.jpg")

    if output_image_path is None:
        output_image_path = os.path.join(execution_ws, "output.jpg")

    Init()

    Inference(
        input_image_path=input_image_path,
        output_image_path=output_image_path,
        show_image=show_image,
        confidence=confidence,
    )

    Release()

    return "Yolo26 Inference Result"

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Process a single image path.")
    parser.add_argument('--input_image_path', help='Path to the input image', default=None)
    #input_image_path, output_image_path
    parser.add_argument('--output_image_path', help='Path to the output image', default=None)
    parser.add_argument('--confidence', help='Score threshold for keeping boxes', type=float, default=0.60)
    args = parser.parse_args()

    main(args.input_image_path, args.output_image_path, confidence=args.confidence)
