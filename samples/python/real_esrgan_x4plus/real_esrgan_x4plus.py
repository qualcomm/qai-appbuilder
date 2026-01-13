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
import cv2
import numpy as np
import torchvision.transforms as transforms
from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from utils.image_processing import (
    pil_resize_pad,
    pil_undo_resize_pad
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

if not "python" in str(execution_ws):
    execution_ws = execution_ws / "python"

if not MODEL_NAME in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"

#load .bin
model_path = model_dir /  "{}.bin".format(MODEL_NAME)
IMAGE_SIZE = 512
#load .dlc
#model_path = model_dir /  "{}.dlc".format(MODEL_NAME)
#IMAGE_SIZE = 128

####################################################################

image_buffer = None
realesrgan = None


# RealESRGan class which inherited from the class QNNContext.
class RealESRGan(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
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
    realesrgan = RealESRGan("realesrgan", str(model_path))

def Inference(input_image_path, output_image_path, show_image = True):
    global image_buffer

    # Read and preprocess the image.
    orig_image = Image.open(input_image_path)
    image, scale, padding = pil_resize_pad(orig_image, (IMAGE_SIZE, IMAGE_SIZE))

    image = np.array(image)
    image = (np.clip(image, 0, 255) / 255.0).astype(np.float32)  # normalization

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output_image = realesrgan.Inference(image)

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()

    output_image = output_image.reshape(IMAGE_SIZE * 4, IMAGE_SIZE * 4, 3)
    
    output_image = np.clip(output_image, 0.0, 1.0)
    output_image = (output_image * 255).astype(np.uint8) # un-normalization
    output_image = ImageFromArray(output_image)

    image_size = (orig_image.size[0] * 4, orig_image.size[1] * 4)
    image_padding = (padding[0] * 4, padding[1] * 4)
    image_buffer = pil_undo_resize_pad(output_image, image_size, scale, image_padding)

    # show & save the result
    image_buffer.save(output_image_path)
    
    if show_image:
        image_buffer.show()

def getGraphName():
    graph_name = realesrgan.getGraphName()
    print("debug_realesrgan,graph_name:",graph_name)
    
def getInputShapes():
    input_shapes = realesrgan.getInputShapes()
    print("debug_realesrgan,input_shapes:",input_shapes)

def getInputDataType():
    input_dataType = realesrgan.getInputDataType()
    print("debug_realesrgan,input_dataType:",input_dataType)

def getOutputShapes():
    output_shapes = realesrgan.getOutputShapes()
    print("debug_realesrgan,output_shapes:",output_shapes)

def getOutputDataType():
    output_dataType = realesrgan.getOutputDataType()
    print("debug_realesrgan,output_dataType:",output_dataType)
    
def getInputName():
    input_name = realesrgan.getInputName()
    print("debug_realesrgan,input_name:",input_name)

def getOutputName():
    output_name = realesrgan.getOutputName()
    print("debug_realesrgan,output_name:",output_name)    

def Release():
    global realesrgan

    # Release the resources.
    del(realesrgan)

def main(input_image_path=None, output_image_path=None, show_image = True):

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
	
    Inference(input_image_path=input_image_path,output_image_path=output_image_path,show_image=show_image)

    Release()
    return "Real ESR Gan Inference Result"

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Process a single image path.")
    parser.add_argument('--input_image_path', help='Path to the input image', default=None)
    #input_image_path, output_image_path
    parser.add_argument('--output_image_path', help='Path to the output image', default=None)
    args = parser.parse_args()
    main(args.input_image_path,args.output_image_path)
