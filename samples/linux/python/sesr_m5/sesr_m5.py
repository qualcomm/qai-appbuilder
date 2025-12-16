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
import torchvision.transforms as transforms
from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from utils.image_processing import (
    pil_resize_pad,
    pil_undo_resize_pad
)
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)


####################################################################

MODEL_ID = "mq938r3rq"
MODEL_NAME = "sesr_m5"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
IMAGE_SIZE = 128

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

image_buffer = None
sesr_m5 = None


# sesr_m5 class which inherited from the class QNNContext.
class Sesr_m5(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)[0]        
        return output_data

def model_download():
    ret = True

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(SOC_ID, MODEL_NAME, model_path, desc=desc, fail=fail)

    if not ret:
        exit()

def Init():
    global sesr_m5

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(qnn_dir, Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for sesr_m5 objects.
    sesr_m5 = Sesr_m5("sesr_m5", model_path)

def Inference(input_image_path, output_image_path, show_image = True):
    global image_buffer
    


    # Read and preprocess the image.
    orig_image = Image.open(input_image_path)
    image, scale, padding = pil_resize_pad(orig_image, (IMAGE_SIZE, IMAGE_SIZE))

    image = np.array(image)
    image = np.clip(image, 0, 255) / 255.0  # normalization

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output_image = sesr_m5.Inference([image])

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

def Release():
    global sesr_m5

    # Release the resources.
    del(sesr_m5)

if __name__ == '__main__':
    Init()

    Inference(os.path.join(execution_ws,"input.jpg"), os.path.join(execution_ws,"output.jpg"))

    Release()
