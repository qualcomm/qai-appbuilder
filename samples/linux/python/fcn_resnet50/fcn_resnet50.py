# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import sys
import os
sys.path.append(".")
sys.path.append("python")
import utils.install as install
import cv2
import numpy as np
import math
import argparse
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)


####################################################################

MODEL_ID = "mqpeoj4on"
MODEL_NAME = "fcn_resnet50"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
IMAGE_SIZE = 520

####################################################################

execution_ws=os.path.dirname(os.path.abspath(__file__))
print(f"Current file directory: {execution_ws}")
qnn_sdk_root = os.environ.get("QNN_SDK_ROOT")
if not qnn_sdk_root:
    print("Error: QNN_SDK_ROOT environment variable is not set.")
    sys.exit(1)

qnn_dir = os.path.join(qnn_sdk_root, "lib/aarch64-oe-linux-gcc11.2")

# if not "python" in execution_ws:
#     execution_ws = execution_ws + "/" + "python"

if not MODEL_NAME in execution_ws:
    execution_ws = execution_ws + "/" + MODEL_NAME

model_dir = execution_ws + "/models"
model_path = model_dir + "/" + MODEL_NAME + ".bin"

####################################################################



def resize_pad(
    image,
    dst_size,
    pad_mode,
    pad_value=0.0,
) :
    """
    Resize and pad image to be shape [..., dst_size[0], dst_size[1]]

    This will not warp or crop the image. It will be resized as large as it can
    possibly be without being cropped and while maintaining aspect ratio.
    The image is then padded so that it's in the center of the returned image "frame"
    of the desired size (dst_size).

    Parameters:
        image: (..., H, W)
            Image to reshape.

        dst_size: (height, width)
            Size to which the image should be reshaped.

    Returns:
        rescaled_padded_image:
            torch.Tensor (..., dst_size[0], dst_size[1])

        scale:
            scale factor between original image and dst_size image

        pad:
            pixels of padding added to the rescaled image: (left_padding, top_padding)

    Based on https://github.com/zmurez/MediaPipePyTorch/blob/master/blazebase.py
    """
    height, width = image.shape[:2]
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

    rescaled_image = cv2.resize(image,(int(new_width),int(new_height)),interpolation=cv2.INTER_LINEAR)
    rescaled_padded_image = np.pad(rescaled_image,((pad_top, pad_bottom),(pad_left, pad_right),(0,0)),mode=pad_mode, constant_values=0)
    padding = (pad_left, pad_top)

    return rescaled_padded_image, scale, padding

def pil_resize_pad(image,dst_size, pad_mode = "constant") :
    torch_image =image/255.
    torch_out_image, scale, padding = resize_pad(
        torch_image,
        dst_size,
        pad_mode=pad_mode,
    )
    pil_out_image = torch_out_image[0]
    return (pil_out_image, scale, padding)   

def create_color_map(num_classes):
    """
    Assign a random color to each class in the dataset to produce a segmentation mask for drawing.

    Inputs:
        num_classes: Number of colors to produce.

    Returns:
        A list of `num_classes` colors in RGB format.
    """
    np.random.seed(42)  # For reproducible results
    color_map = np.random.randint(0, 256, size=(num_classes, 3), dtype=np.uint8)
    color_map[0] = [0, 0, 0]  # Background class, usually black
    return color_map

def undo_resize_pad( image,orig_size_wh,scale,padding):
    """
    Undos the efffect of resize_pad. Instead of scale, the original size
    (in order width, height) is provided to prevent an off-by-one size.
    """
    width, height = orig_size_wh

    rescaled_image = cv2.resize(image,None, fx=1/scale, fy=1/scale,interpolation=cv2.INTER_LINEAR)
    scaled_padding = [int(round(padding[0] / scale)), int(round(padding[1] / scale))]
    cropped_image = rescaled_image[
        scaled_padding[1] : scaled_padding[1] + height,
        scaled_padding[0] : scaled_padding[0] + width,
        ...
    ]

    return cropped_image

def Init():
    global fcn_resnet50

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(qnn_dir, Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for fcn_resnet50 objects.
    fcn_resnet50 = Fcn_resnet50("fcn_resnet50", model_path)

def model_download():
    ret = True

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(MODEL_ID, model_path, desc=desc, fail=fail)

    if not ret:
        exit()

def Inference(frame,invoke_nums):
    h,w,_=frame.shape
    inputs = cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)
    inputs = inputs/255.
    image1,scale,padding = resize_pad(inputs,(IMAGE_SIZE,IMAGE_SIZE),pad_mode="constant")
    pil_out_image = image1.copy()
    img_input =pil_out_image.astype(np.float32)

    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output = fcn_resnet50.Inference([img_input])

    PerfProfile.RelPerfProfileGlobal()

    # 假设 predictions 是一维数组，长度为 520×520
    predictions = np.array(output[0]).astype(np.int32)
    predictions = np.clip(predictions, 0, 20)  # 防止越界

    color_map = create_color_map(21)
    mask_img = color_map[predictions]  # shape: (270400, 3)
    mask_img = mask_img.reshape((520, 520, 3))  # 恢复为图像格式

    # 如果 pil_out_image 是 PIL 图像，先转换为 NumPy 数组
    if not isinstance(pil_out_image, np.ndarray):
        pil_out_image = np.array(pil_out_image)

    # 融合图像
    blended_image = cv2.addWeighted((pil_out_image * 255).astype(np.uint8), 0.5, mask_img, 0.5, 0)

    result_img = undo_resize_pad(blended_image, (w,h), scale, padding)
    result_img = cv2.cvtColor(result_img,cv2.COLOR_RGB2BGR)

    return mask_img,result_img

class Fcn_resnet50(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)    
        return output_data

def Release():
    global fcn_resnet50

    # Release the resources.
    del(fcn_resnet50)

def main(input = None):

    if input is None:
        input = os.path.join(execution_ws, "input.jpg")
        print(execution_ws)

    Init()

    frame = cv2.imread(input) 
    mask_img,result_img = Inference(frame, 10)
    
    cv2.imwrite(f"{execution_ws}/result_img.jpg",result_img)
    cv2.imwrite(f"{execution_ws}/mask_img.jpg",mask_img)

    Release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a single image path.")
    parser.add_argument('--image', help='Path to the image', default=None)
    args = parser.parse_args()

    main(args.image)