# QAI AppBuilder for Ubuntu 24.04 (QCS6490)

QAI AppBuilder supports multiple operating systems, including Windows, Android, and Linux. This guide explains how to enable QAI AppBuilder on Ubuntu 24.04 with the Qualcomm QCS6490 SoC.

## Prerequisites
1. Qualcomm QCS6490 development kit
2. Ubuntu 24.04

## Steps

### 1. Clone the Repository
Clone the required library functions:
```bash
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```

### 2. Download QNN SDK
Download and install the Qualcomm® AI Runtime SDK from:

https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK

> **Note:**  
> This module is intended for Linux OS, version 2.38.0.250901, with the architecture set to x86.  
> Although the package is labeled for x86, it also contains dynamic libraries for the aarch64 architecture.  
> Ensure you select the correct dynamic libraries for your hardware during deployment and runtime to avoid compatibility issues.
> The downloaded file will have a *.qik extension. For usage instructions, refer to the official documentation:  
> https://docs.qualcomm.com/bundle/publicresource/topics/80-77512-1/hexagon-dsp-sdk-install-addons-linux.html?product=1601111740010422

### 3. Build the Project
Create a build directory and compile the project:
```bash
python setup.py bdist_wheel
```
For more details, see [Build](../BUILD.md).

### 4. Set Environment Variables
Set the following environment variables:
```bash
export QNN_SDK_ROOT=<path_to_qnn_sdk>
export LD_LIBRARY_PATH=$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2
export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-v68/unsigned
```
Replace `<path_to_qnn_sdk>` with the actual path where you extracted the QNN SDK.


### 5. Run Sample Models
Navigate to the sample directory:

The following table lists available sample models, their descriptions, and instructions to run each example:

| Example                | Description                                   | How to Run                                      |
|------------------------|-----------------------------------------------|-------------------------------------------------|
| aotgan                 | AOTGAN image inpainting                       | `python aotgan/aotgan.py`                       |
| beit                   | BEiT image classification                     | `python beit/beit.py`                           |
| depth_anything         | Depth Anything depth estimation               | `python depth_anything/depth_anything.py`       |
| easy_ocr               | EasyOCR text recognition                      | `python easy_ocr/easy_ocr.py`                   |
| face_attrib_net        | Face attribute recognition                    | `python face_attrib_net/face_attrib_net.py`     |
| facemap_3dmm           | 3DMM face mapping                             | `python facemap_3dmm/facemap_3dmm.py`           |
| googlenet              | GoogLeNet image classification                | `python googlenet/googlenet.py`                 |
| inception_v3           | Inception V3 image classification             | `python inception_v3/inception_v3.py`           |
| lama_dilated           | LaMa Dilated image inpainting                 | `python lama_dilated/lama_dilated.py`           |
| mediapipe_hand         | MediaPipe Hand landmark detection             | `python mediapipe_hand/mediapipe_hand.py`       |
| nomic_embed_text       | Nomic text embedding                          | `python nomic_embed_text/nomic_embed_text.py`   |
| openai_clip            | OpenAI CLIP model                             | `python openai_clip/openai_clip.py`             |
| openpose               | OpenPose human pose estimation                | `python openpose/openpose.py`                   |
| quicksrnetmedium       | QuickSRNetMedium image super-resolution       | `python quicksrnetmedium/quicksrnetmedium.py`   |
| real_esrgan_general_x4v3 | RealESRGAN General X4V3 super-resolution   | `python real_esrgan_general_x4v3/real_esrgan_general_x4v3.py` |
| real_esrgan_x4plus     | RealESRGAN X4Plus image super-resolution      | `python real_esrgan_x4plus/real_esrgan_x4plus.py` |
| resnet_3d              | 3D ResNet video classification                | `python resnet_3d/resnet_3d.py`                 |
| stable_diffusion_v1_5  | Stable Diffusion v1.5 text-to-image           | `python stable_diffusion_v1_5/stable_diffusion_v1_5.py` |
| stable_diffusion_v2_1  | Stable Diffusion v2.1 text-to-image           | `python stable_diffusion_v2_1/stable_diffusion_v2_1.py` |
| unet_segmentation      | UNet image segmentation                       | `python unet_segmentation/unet_segmentation.py` |
| whisper_base_en        | Whisper Base (English) speech recognition     | `python whisper_base_en/whisper_base_en.py`     |
| whisper_tiny_en        | Whisper Tiny (English) speech recognition     | `python whisper_tiny_en/whisper_tiny_en.py`     |
| yamnet                 | YAMNet audio event classification             | `python yamnet/yamnet.py`                       |
| yolov8_det             | YOLOv8 object detection                       | `python yolov8_det/yolov8_det.py`               |

> **Note:**  
> Ensure you are in the `samples/python` directory before running any example.