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
> This module is intended for Linux OS, version 2.34.0.250424, with the architecture set to x86.  
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
| convnext_base          | ConvNeXt Base image classification            | `python convnext_base/convnext_base.py`         |
| convnext_tiny          | ConvNeXt Tiny image classification            | `python convnext_tiny/convnext_tiny.py`         |
| efficientnet_b0        | EfficientNet B0 image classification          | `python efficientnet_b0/efficientnet_b0.py`     |
| efficientnet_b4        | EfficientNet B4 image classification          | `python efficientnet_b4/efficientnet_b4.py`     |
| efficientnet_v2_s      | EfficientNet V2 Small image classification    | `python efficientnet_v2_s/efficientnet_v2_s.py` |
| fcn_resnet50           | FCN-ResNet50 semantic segmentation            | `python fcn_resnet50/fcn_resnet50.py`           |
| googlenet              | GoogLeNet image classification                | `python googlenet/googlenet.py`                 |
| inception_v3           | Inception V3 image classification             | `python inception_v3/inception_v3.py`           |
| levit                  | LeViT image classification                    | `python levit/levit.py`                         |
| quicksrnetmedium       | QuickSRNetMedium image super-resolution       | `python quicksrnetmedium/quicksrnetmedium.py`   |
| real_esrgan_general_x4v3 | RealESRGAN General X4V3 super-resolution   | `python real_esrgan_general_x4v3/real_esrgan_general_x4v3.py` |
| real_esrgan_x4plus     | RealESRGAN X4Plus image super-resolution      | `python real_esrgan_x4plus/real_esrgan_x4plus.py` |
| regnet                 | RegNet image classification                   | `python regnet/regnet.py`                       |
| sesr_m5                | SESR-M5 image super-resolution                | `python sesr_m5/sesr_m5.py`                     |
| shufflenet_v2          | ShuffleNet V2 image classification            | `python shufflenet_v2/shufflenet_v2.py`         |
| squeezenet1_1          | SqueezeNet1.1 image classification            | `python squeezenet1_1/squeezenet1_1.py`         |
| utils                  | Utility scripts for preprocessing/postprocessing | `python utils/utils.py`                      |
| vit                    | Vision Transformer image classification       | `python vit/vit.py`                             |
| wideresnet50           | WideResNet50 image classification             | `python wideresnet50/wideresnet50.py`           |
| xlsr                   | XLSR speech recognition                       | `python xlsr/xlsr.py`                           |
| yolov8_det             | YOLOv8 object detection                       | `python yolov8_det/yolov8_det.py`               |

> **Note:**  
> Ensure you are in the `samples/python` directory before running any example.