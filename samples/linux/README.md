# QAI AppBuilder for Ubuntu 24.04

## Overview

This guide demonstrates how to develop AI applications on Qualcomm Dragonwing™ **IQ9075** and **QCS6490** platforms using QAI AppBuilder. QAI AppBuilder provides both Python and C++ interfaces, allowing you to build AI applications with just a few lines of code.

## Quick Start

### Prerequisites

Before you begin, ensure you have the following:

- **Hardware:** Qualcomm Dragonwing™ IQ9075 or QCS6490 development board
- **OS:** Ubuntu 24.04 installed and configured
- **Python:** Python 3.8 or higher (Python 3.12 recommended)
- **Tools:** Git and sudo access
- **Network:** Internet connection for downloading dependencies

**Clone the repository with submodules:**

```bash
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
cd ai-engine-direct-helper
```

### Download QNN SDK

Download the Qualcomm® AI Runtime (QAIRT) SDK on your device. This SDK includes the required QNN runtime libraries for AI model execution.

**Download Link:** [QAIRT v2.40.0.251030](https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.40.0.251030/v2.40.0.251030.zip)

```bash
# Download QAIRT SDK package 
wget https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.40.0.251030/v2.40.0.251030.zip

# Extract the runtime libraries
unzip v2.40.0.251030.zip

# Verify extraction
ls v2.40.0.251030/
```


### Set Environment Variables

Configure the required environment variables on your device. Replace `<path_to_v2.40.0.251030>` with the actual path to your extracted QNN SDK directory.

**Common variables for both platforms:**

```bash
export QNN_SDK_ROOT=<path_to_v2.40.0.251030>
export LD_LIBRARY_PATH=$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2:$LD_LIBRARY_PATH
```

**Platform-specific configuration:**

- **For QCS6490 devices (Hexagon v68):**
  ```bash
  export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-v68/unsigned
  ```

- **For IQ9075 devices (Hexagon v73):**
  ```bash
  export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-v73/unsigned
  ```

### Run Python API Samples

QAI AppBuilder provides multiple examples of AI applications developed using Python, covering scenarios such as image super-resolution, object detection, and image classification. Follow the steps below to run the Python API examples and quickly get started with these applications.


#### 1. Install Python Dependencies

Upgrade build tooling and install required Python packages:

```bash
# Install system dependencies
sudo apt update
sudo apt install -y cmake build-essential python3.12-dev

# Install core Python packages
pip install requests==2.32.3 \
            py3-wget==1.0.12 \
            tqdm==4.67.1 \
            importlib-metadata==8.5.0 \
            qai-hub==0.30.0 \
            opencv-python==4.10.0.82 \
            gradio

pip install transformers==4.45.0 torch==2.9.1
pip install torchvision>=0.9.0
pip install qwen-vl-utils
```

#### 2. Build QAI AppBuilder Python and C/C++ Libraries

Build the QAI AppBuilder libraries from the project root directory. Choose the command based on your platform:

**For QCS6490 (Hexagon v68):**
```bash
python setup.py bdist_wheel --toolchains aarch64-oe-linux-gcc11.2 --hexagonarch 68
```

**For IQ9075 (Hexagon v73):**
```bash
python setup.py bdist_wheel --toolchains aarch64-oe-linux-gcc11.2 --hexagonarch 73
```

> **Note:** The build process may take several minutes. The output wheel file will be created in the `dist/` directory.

#### 3. Install QAI AppBuilder Wheel Package

Install the built wheel package:

```bash
python -m pip install dist/qai_appbuilder-*.whl
```

> **Note:** The version number may vary based on your QNN SDK version.

**Verify Installation:**
```bash
python -c "import qai_appbuilder; print('QAI AppBuilder installed successfully')"
```

#### 4. Run Python Samples

Navigate to the samples directory and set the required preload library:

```bash
export LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libtbb.so.12
cd samples/linux/python
```

**Available Samples:**

The following table lists all available sample models with descriptions and execution commands:

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

> **Note:** Ensure you are in the `samples/linux/python` directory before running any example. Each sample will automatically download its required model on first run.


### QAI AppBuilder C++ API Sample

This example demonstrates AI model inference using the QAI AppBuilder C++ API. The implementation covers:

- Initializing the QAI AppBuilder runtime environment
- Loading pre-trained AI models
- Preparing input data for inference
- Executing model inference
- Processing and retrieving output results

**Basic Usage Pattern:**

```cpp
#include "LibAppBuilder.hpp"

// Initialize AppBuilder and set logging level
LibAppBuilder libAppBuilder;
SetLogLevel(2);

// Prepare buffers for input and output data
std::vector<uint8_t*> inputBuffers;
std::vector<uint8_t*> outputBuffers;
std::vector<size_t> outputSize;

// Initialize the model with required paths
libAppBuilder.ModelInitialize(model_name, model_path, backend_lib_path, system_lib_path);

// Prepare input data
// TODO: Load and preprocess your input data into inputBuffers

// Run inference
libAppBuilder.ModelInference(model_name, inputBuffers, outputBuffers, outputSize);

// Process inference results
// TODO: Use the data in outputBuffers for your application

// Clean up allocated memory
for (size_t j = 0; j < outputBuffers.size(); j++) {
    free(outputBuffers[j]);
}
outputBuffers.clear();
outputSize.clear();

libAppBuilder.ModelDestroy(model_name);
```


### Vision Language Model Examples

QAI AppBuilder provides examples for building Vision Language Model (VLM) applications that combine image and video understanding with natural language processing.

#### Running VLM Examples

Navigate to the VLM samples directory and run the demo:

```bash
cd samples/linux/python/qwen2_vl
python demo_app.py <model_path>
```

**Parameters:**
- `<model_path>`: Path to the directory containing the Qwen2-VL QNN model files

**VLM Capabilities:**
- Image/Video/Web camera input processing
- Multimodal inference combining vision and language
- Natural language output generation
- Qwen2-VL-2B-Instruct model support

**For detailed setup and usage instructions, see:** [Qwen2-VL Demo (Linux Python)](./python/qwen2_vl/README.md)


## Advanced Application Examples

In addition to the computer vision samples above, QAI AppBuilder provides advanced examples for building production-ready AI applications.

> **Note:** These advanced examples currently support **Qualcomm Dragonwing™ IQ9075** only.

1. **Chat Application with OpenAI-Compatible API**: Build conversational chat WebUI applications using OpenAI-compatible APIs for seamless integration.

2. **LangFlow Low-Code Framework**: Deploy and run the LangFlow low-code framework on IQ9075 for rapid AI application development.

### Advanced Example 1: Chat Application with OpenAI-Compatible API

Build a chat application powered by large language models (LLMs) using OpenAI-compatible APIs.

**Steps to Run:**

1. **Navigate to the launcher directory:**
   ```bash
   cd tools/launcher_linux
   ```

2. **Run the setup and launch scripts in sequence:**
   ```bash
   bash ./1.Install_QAI_Appbuilder.sh
   bash ./2.Install_LLM_Models.sh
   bash ./3.Start_WebUI.sh
   ```

3. **Access the WebUI:**
   Open your browser and navigate to `http://localhost:7860` (default port)

### Advanced Example 2: LangFlow Low-Code Framework

Deploy LangFlow, a visual low-code framework for building AI applications with drag-and-drop components.

**Steps to Run:**

1. **Start the LLM Service:**
   ```bash
   cd tools/launcher_linux
   bash ./4.Start_GenieAPIService.sh
   ```

2. **Install and Launch LangFlow:**
   ```bash
   bash ./5.Install_LangFlow.sh
   bash ./6.Start_LangFlow.sh
   ```

Once started, access the LangFlow web interface to design and deploy your AI workflows visually.


## Additional Resources

For detailed documentation and tutorials:

- **[Python API Documentation](../../docs/python.md)** - Complete Python API reference
- **[Python ARM64 Guide](../../docs/python_arm64.md)** - ARM64-specific development guide
- **[Interactive Tutorial](../../docs/tutorial.ipynb)** - Jupyter notebook with step-by-step examples
- **[User Guide](../../docs/user_guide.md)** - Comprehensive usage guide

## Support

For issues, questions, or contributions:
- **Issue Tracker**: [GitHub Issues](https://github.com/quic/ai-engine-direct-helper/issues)
- **Documentation**: [Project README](../../README.md)
- **Community**: Check existing issues before creating new ones

## Troubleshooting

### Common Issues and Solutions

**Issue: `undefined symbol: _ZN3tbb6detail2r114execution_slotEPKNS0_2d114execution_dataE`**

This is a TBB (Threading Building Blocks) library conflict. Fix using one of these options:

```bash
# Option 1: Remove conflicting libtbb-dev
sudo apt remove libtbb-dev

# Option 2: Set LD_PRELOAD to use specific TBB version
export LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libtbb.so.12
```