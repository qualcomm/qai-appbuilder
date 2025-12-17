# QAI AppBuilder for Ubuntu 24.04 

## Overview

This guide shows how to develop AI applications on Qualcomm Dragonwing™ **IQ9075** and **QCS6490** using QAI AppBuilder. It highlights official support for both SoCs and covers three key topics:

1. **Chat Application with OpenAI-Compatible API**: Build a conversational chat WebUI application using OpenAI-compatible APIs for seamless integration.

2. **LangFlow Low-Code Framework Sample**: Deploy and run the LangFlow low-code framework on the IQ9075 development board for rapid AI application development.

3. **AI Model Inference with Python API Sample**: Learn how to use the QAI AppBuilder Python interface to perform AI model inference on the IQ9075 SoC.


## Platform Support

| Component                         | IQ9075          | QCS6490        |
|-----------------------------------|-----------------|----------------|
| Core SDK & Python API             | Supported       | Supported      |
| Chat Application (OpenAI API)     | Supported       | Not yet        |
| LangFlow Low-Code Framework       | Supported       | Not yet        |

Notes:
- Supported SoCs: Qualcomm Dragonwing™ IQ9075 and QCS6490
- Chat and LangFlow samples currently target IQ9075; Python API inference supports both SoCs.


## Getting Started

### Prerequisites

Before you begin, ensure you have the following:

- Qualcomm Dragonwing™ IQ9075 or QCS6490 development board (both supported)
- Ubuntu 24.04 installed and configured
- QAI AppBuilder SDK installed
- Python 3.8 or higher

Clone the required library functions:
```bash
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```

### Example 1: Chat Application with OpenAI-Compatible API

> **Note:**  
> This chat application currently supports IQ9075.QCS6490 not supports yet.

```bash
cd tools/launcher_linux
```
Run below commands sequences

```bash
bash ./1.Install_QAI_Appbuilder.sh
bash ./2.Install_LLM_Models.sh
bash ./3.Start_WebUI.sh

```

### Example 2: LangFlow Low-Code Framework Sample

> **Note:**  
> This LangFlow sample currently supports IQ9075. QCS6490 not supports yet.

#### Step 1: start llm service
```bash
cd tools/launcher_linux
bash ./4.Start_GenieAPIService.sh

```
#### Step 2: install and start LangFlow application
```bash
bash ./5.Install_LangFlow.sh
bash ./6.Start_LangFlow.sh

```

### Example 3: AI Model Inference with Python API

This example demonstrates how to use Python API to perform inference on CV AI models. The models used in this example are automatically downloaded via network requests within the Python script.

### Model Selection Based on Device

- **QCS6490 Device**: When the example script runs on a QCS6490 device, the automatically downloaded model is a quantized INT8 model optimized for integer inference.

- **IQ9075 Device**: When the example script runs on an IQ9075 device, the automatically downloaded model is a float16 model for half-precision floating-point inference.


### 1. Download QNN SDK
Download and install the Qualcomm® AI Runtime SDK from:


https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK

> **Note:**  
> The current Python demo requires QNN version 2.39.x or higher.
> This module is intended for Linux OS, version 2.38.0.250901, with the architecture set to x86.  
> Although the package is labeled for x86, it also contains dynamic libraries for the aarch64 architecture.  
> Ensure you select the correct dynamic libraries for your hardware during deployment and runtime to avoid compatibility issues.
> The downloaded file will have a *.qik extension. For usage instructions, refer to the official documentation:  
> https://docs.qualcomm.com/bundle/publicresource/topics/80-77512-1/hexagon-dsp-sdk-install-addons-linux.html?product=1601111740010422

### 2. Install basic Python dependencies:
Run below command in Windows terminal:
```
pip install requests==2.32.3 py3-wget==1.0.12 tqdm==4.67.1 importlib-metadata==8.5.0 qai-hub==0.30.0 opencv-python==4.10.0.82 torch>=1.8.0 torchvision>=0.9.0
```

### 3. Set Environment Variables

**demo requires QNN version 2.39.x or higher**

Set the following environment variables:
```bash
export QNN_SDK_ROOT=<path_to_qnn_sdk>
export LD_LIBRARY_PATH=$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2
```

- If the development board’s SoC is QCS6490, run the following command.
```bash
export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-v68/unsigned
```

- if it is IQ9075, run the following command to set the environment variable path

```bash
export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-v75/unsigned
```

Replace `<path_to_qnn_sdk>` with the actual path where you extracted the QNN SDK.

### 4. Build the Project

Create a build directory and compile the project:

- If the development board’s SoC is QCS6490, run the following command.
```bash
python setup.py --toolchains aarch64-oe-linux-gcc11.2 --hexagonarch 68 bdist_wheel

```

- if it is IQ9075, run the following command to set the environment variable path
```bash
python setup.py --toolchains aarch64-oe-linux-gcc11.2 --hexagonarch 75 bdist_wheel

```

### 5. Install Qai Appubuilder Linux wheel
```bash
pip install dist/qai_appbuilder-2.38.0-cp312-cp312-linux_aarch64.whl 

```

### 6. Run Sample Models

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
| xlsr                   | XLSR super-resolution                       | `python xlsr/xlsr.py`                           |
| yolov8_det             | YOLOv8 object detection                       | `python yolov8_det/yolov8_det.py`               |

> **Note:**  
> Ensure you are in the `samples/linux/python` directory before running any example.






