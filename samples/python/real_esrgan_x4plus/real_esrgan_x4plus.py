# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
import argparse
sys.path.append(".")
sys.path.append("python")

import utils.install as install
import cv2  # noqa: F401
import numpy as np
import torchvision.transforms as transforms  # noqa: F401
from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from utils.image_processing import (
    pil_resize_pad,
    pil_undo_resize_pad,
)
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)
from pathlib import Path

####################################################################
MODEL_ID = "mnz1l2exq"
MODEL_NAME = "real_esrgan_x4plus"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
####################################################################

execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"
if "python" not in str(execution_ws):
    execution_ws = execution_ws / "python"
if MODEL_NAME not in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"

# Load .bin
model_path = model_dir / f"{MODEL_NAME}.bin"
# Load .dlc
#model_path = model_dir / f"{MODEL_NAME}.dlc"

# Fallback image size; we will override from model input shape at runtime.
IMAGE_SIZE = 512

####################################################################
image_buffer = None
realesrgan = None


def _guess_layout_from_shape(shape4):
    """Infer layout from a 4D input shape.

    shape4 should be like [N, H, W, C] (NHWC) or [N, C, H, W] (NCHW).
    """
    if len(shape4) != 4:
        return None

    # Heuristic: channels are typically 1/3/4.
    c_candidates = {1, 3, 4}
    if shape4[1] in c_candidates and shape4[-1] not in c_candidates:
        return "NCHW"
    if shape4[-1] in c_candidates and shape4[1] not in c_candidates:
        return "NHWC"

    # If ambiguous, prefer NCHW if second dim looks like channels.
    if shape4[1] in c_candidates:
        return "NCHW"
    return "NHWC"


def _set_image_size_from_model():
    """Update IMAGE_SIZE using the model's expected input shape."""
    global IMAGE_SIZE
    try:
        shapes = realesrgan.getInputShapes()
        if shapes and len(shapes[0]) == 4:
            layout = _guess_layout_from_shape(shapes[0])
            if layout == "NCHW":
                IMAGE_SIZE = int(shapes[0][2])
            else:
                IMAGE_SIZE = int(shapes[0][1])
            print(f"debug_realesrgan,detected_input_layout: {layout}")
            print(f"debug_realesrgan,using_IMAGE_SIZE: {IMAGE_SIZE}")
    except Exception as e:
        print(f"warn: failed to infer IMAGE_SIZE from model input shape: {e}")


# RealESRGan class which inherited from the class QNNContext.
class RealESRGan(QNNContext):
    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data


def model_download():
    ret = True
    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(MODEL_ID, model_path, desc=desc, fail=fail)
    if not ret:
        exit()


def Init():
    global realesrgan
    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(str(qnn_dir), Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for RealESRGan objects.
    realesrgan = RealESRGan("realesrgan", str(model_path), deviceID=0, coreIdsStr="0")

    # IMPORTANT: adapt IMAGE_SIZE & layout based on the model.
    _set_image_size_from_model()


def Inference(input_image_path, output_image_path, show_image=True):
    global image_buffer

    # Read and preprocess the image.
    orig_image = Image.open(input_image_path).convert("RGB")

    # Resize+pad to model's expected spatial size.
    image, scale, padding = pil_resize_pad(orig_image, (IMAGE_SIZE, IMAGE_SIZE))

    # Convert to float32 [0,1]
    image = np.array(image, dtype=np.float32)
    image = (np.clip(image, 0, 255) / 255.0).astype(np.float32)

    # Prepare tensor in the layout expected by the model.
    input_shapes = realesrgan.getInputShapes()
    layout = None
    if input_shapes and len(input_shapes[0]) == 4:
        layout = _guess_layout_from_shape(input_shapes[0])

    if layout == "NCHW":
        # HWC -> NCHW with batch
        input_tensor = np.transpose(image, (2, 0, 1))[None, ...]
    else:
        # Default NHWC with batch
        input_tensor = image[None, ...]

    input_tensor = np.ascontiguousarray(input_tensor, dtype=np.float32)

    print("debug_realesrgan,prepared_input.shape:", input_tensor.shape)

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output_tensor = realesrgan.Inference(input_tensor)

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()

    print("debug_realesrgan,raw_output.shape:", getattr(output_tensor, 'shape', None))

    # Convert output tensor to HWC float in [0,1].
    out = output_tensor
    if isinstance(out, np.ndarray):
        if out.ndim == 4:
            out = out[0]
        if layout == "NCHW":
            # CHW -> HWC
            if out.ndim == 3 and out.shape[0] in (1, 3, 4):
                out = np.transpose(out, (1, 2, 0))
        else:
            # NHWC already
            pass

    # Now `out` should be HWC.
    out = np.clip(out, 0.0, 1.0)
    out_u8 = (out * 255.0).astype(np.uint8)

    output_image = ImageFromArray(out_u8)

    # Undo padding/scale on the x4 output.
    image_size = (orig_image.size[0] * 4, orig_image.size[1] * 4)
    image_padding = (padding[0] * 4, padding[1] * 4)
    image_buffer = pil_undo_resize_pad(output_image, image_size, scale, image_padding)

    # show & save the result
    image_buffer.save(output_image_path)
    if show_image:
        image_buffer.show()


def getGraphName():
    graph_name = realesrgan.getGraphName()
    print("debug_realesrgan,graph_name:", graph_name)


def getInputShapes():
    input_shapes = realesrgan.getInputShapes()
    print("debug_realesrgan,input_shapes:", input_shapes)


def getInputDataType():
    input_dataType = realesrgan.getInputDataType()
    print("debug_realesrgan,input_dataType:", input_dataType)


def getOutputShapes():
    output_shapes = realesrgan.getOutputShapes()
    print("debug_realesrgan,output_shapes:", output_shapes)


def getOutputDataType():
    output_dataType = realesrgan.getOutputDataType()
    print("debug_realesrgan,output_dataType:", output_dataType)


def getInputName():
    input_name = realesrgan.getInputName()
    print("debug_realesrgan,input_name:", input_name)


def getOutputName():
    output_name = realesrgan.getOutputName()
    print("debug_realesrgan,output_name:", output_name)


def Release():
    global realesrgan
    # Release the resources.
    del realesrgan


def main(input_image_path=None, output_image_path=None, show_image=True):
    if input_image_path is None:
        input_image_path = execution_ws / "input.jpg"
    if output_image_path is None:
        output_image_path = execution_ws / "output.png"

    Init()

    # Sample code for using these new APIs.
    getInputShapes()
    getInputDataType()
    getOutputShapes()
    getOutputDataType()
    getGraphName()
    getInputName()
    getOutputName()

    Inference(input_image_path=input_image_path, output_image_path=output_image_path, show_image=show_image)

    Release()
    return "Real ESR Gan Inference Result"


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Process a single image path.")
    parser.add_argument('--input_image_path', help='Path to the input image', default=None)
    parser.add_argument('--output_image_path', help='Path to the output image', default=None)
    parser.add_argument('--no_show', help='Do not pop up image viewer', action='store_true')
    args = parser.parse_args()

    main(args.input_image_path, args.output_image_path, show_image=(not args.no_show))
