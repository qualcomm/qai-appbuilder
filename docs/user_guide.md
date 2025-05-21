# User Guide

## Environment Setup

### 1. Libraries from QNN SDK:

<b>We need below libraries from QNN SDK for using AppBuilder on Snapdragon X Elite(Windows on Snapdragon device):</b>

If use x64 Python, use the libraries below from QNN SDK:
```
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\arm64x-windows-msvc\QnnHtp.dll  (backend for running model on HTP)
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\arm64x-windows-msvc\QnnCpu.dll  (backend for running model on CPU)
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\arm64x-windows-msvc\QnnHtpPrepare.dll
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\arm64x-windows-msvc\QnnSystem.dll
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\arm64x-windows-msvc\QnnHtpV73Stub.dll
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\hexagon-v73\unsigned\libQnnHtpV73Skel.so
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\hexagon-v73\unsigned\libqnnhtpv73.cat
```

If use ARM64 Python, use the libraries below from QNN SDK(ARM64 Python has better performance in Snapdragon X Elite platform):
```
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\aarch64-windows-msvc\QnnHtp.dll  (backend for running model on HTP)
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\aarch64-windows-msvc\QnnCpu.dll  (backend for running model on CPU)
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\aarch64-windows-msvc\QnnHtpPrepare.dll
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\aarch64-windows-msvc\QnnSystem.dll
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\aarch64-windows-msvc\QnnHtpV73Stub.dll
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\hexagon-v73\unsigned\libQnnHtpV73Skel.so
C:\Qualcomm\AIStack\QAIRT\{SDK Version}\lib\hexagon-v73\unsigned\libqnnhtpv73.cat
```

We can copy these libraries to one folder. E.g.: ```C:\<Project Name>\qnn\``` <br>

### 2. Python and common python extensions: 
Get ARM64 version 'python-3.12.6-arm64.exe' from below link and install it. Make sure to add Python to your PATH environment.
https://www.python.org/ftp/python/3.12.6/python-3.12.6-arm64.exe

Get common Python extensions such as OpenCV, NumPy, Pillow from here:
https://github.com/cgohlke/win_arm64-wheels/releases/download/v2024.6.15/2024.6.15-experimental-cp312-win_arm64.whl.zip
Please to use numpy-1.26.4, do not use numpy-2.0.0.
```
pip install numpy-1.26.4-cp312-cp312-win_arm64.whl
pip install opencv_python_headless-4.10.0.82-cp39-abi3-win_arm64.whl
pip install pillow-10.3.0-cp312-cp312-win_arm64.whl
```

Get PyQt6 from here, refer to the *Notes* below on compiling PyQt6_sip:
https://github.com/RockLakeGrass/Windows-on-ARM64-Toolchain/tree/main/Python/packages/PyQt/PyQt6

### 3. PyTorch, TorchVision, ONNX, ONNX Runtime:
If need these Python extensioins for ARM64 Python, you need compile them by yourselves. If need support on how to compile them, you can contact with us.

### 4. MSVC library:
You need ARM64 version 'msvcp140.dll' from 'Microsoft Visual C++ 2022 Redistributable (Arm64)'. You can download it from here and install it:
https://aka.ms/arm64previewredist/

### 5. Notes: <br>
a. For C++(Visual Studio) projects, you need to set 'Runtime Library' to 'Multi-threaded DLL (/MD)'. Please refer to below link for detailed information:
https://learn.microsoft.com/en-us/cpp/build/reference/md-mt-ld-use-run-time-library?view=msvc-170

b. Plese use the API *LogLevel.SetLogLevel()* for Python and *SetLogLevel()* for C++ to initialize the log function before you call any other APIs. 

c. If using Python 3.11.5, get OpenCV from here:
https://github.com/RockLakeGrass/Windows-on-ARM64-Toolchain/tree/main/Python/packages/opencv-python

d. PyQt6 install:
If using Python 3.12.6, you perhaps need to setup compile environment according to below link for compiling PyQt6_sip: 13.4.0:
https://github.com/quic/ai-engine-direct-helper/tree/main?tab=readme-ov-file#build

Steps to install PyQt6 for Python 3.12.6:
1. Download PyQt6-6.3.1-cp37-abi3-win_arm64.whl & PyQt6_Qt6-6.3.1-py3-none-win_arm64.whl from below link:
https://github.com/RockLakeGrass/Windows-on-ARM64-Toolchain/tree/main/Python/packages/PyQt/PyQt6/PyQt6-6.3.1
2. Use below commands to install PyQt6:

```
pip install PyQt6-6.3.1-cp37-abi3-win_arm64.whl
pip install PyQt6_Qt6-6.3.1-py3-none-win_arm64.whl
pip install PyQt6_sip==13.4.0
```
You can get PyQt6_sip for Python 3.11.5 from here directly:
https://github.com/RockLakeGrass/Windows-on-ARM64-Toolchain/blob/main/Python/packages/PyQt/PyQt6/PyQt6-sip/PyQt6_sip-13.4.0-cp311-cp311-win_arm64.whl


## API from AppBuilder Python binding extension for Python projects.<br>
There're several Python classes from this extension:
- QNNContext - The context of QNN model, used to initialize the QNN model, run the inference and destroy the model resource.
- QNNContextProc - It's similar with QNNContext but support load the model into a separate processes. [*Depress*]
- QNNShareMemory - It's used to create processes share memory while using *QNNContextProc*.
- QNNConfig - It's for configuring  QNN SDK libraries path, runtime(CPU/HTP), log leverl, profiling level.
- PerfProfile - Set the HTP perf profile.
## Sample Code(Python)

```
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)
import os

execution_ws = os.getcwd()
des_dir = execution_ws + "\\qnn"

def SetQNNConfig():
    QNNConfig.Config(des_dir, Runtime.HTP, LogLevel.DEBUG, ProfilingLevel.BASIC)

class TextEncoder(QNNContext):
    #@timer
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)[0]

        # Output of Text encoder should be of shape (1, 77, 768)
        output_data = output_data.reshape((1, 77, 768))
        return output_data

SetQNNConfig()  # We need to call this function to configure the basic environment before using any other AppBuilder functions.

model_text_encoder  = "text_encoder"
text_encoder_model = "models\\text_encoder_quantized.serialized.v73.bin"

# Instance for TextEncoder 
text_encoder = TextEncoder(model_text_encoder, text_encoder_model)

# Burst the HTP before inference.
PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

# Run the inference of text encoder on tokens.
user_text_embedding = text_encoder.Inference(tokens)

# Release the HTP.
PerfProfile.RelPerfProfileGlobal()

# Destroy the model and free the memory resource.
del(text_encoder)
```

## API from 'libappbuilder.dll' for C++ projects.

##### bool LibAppBuilder::ModelInitialize(...) <br>
*std::string model_name*: Model name such as "unet", "text_encoder", "controlnet_canny". Model name must be unique for different model files. <br>
*std::string proc_name*: This is an optional parameter, needed just when you want the model to be executed in a separate process. If use process name, this model will be loaded in 'QAIAppSvc' service process. One service process can load many models. There can be many service processes. <br>
*std::string model_path*: The path of model. <br>
*std::string backend_lib_path*: The path of 'QnnHtp.dll' <br>
*std::string system_lib_path*: The path of 'QnnSystem.dll' <br>

##### bool LibAppBuilder::ModelInference(...) <br>
*std::string model_name*: Model name used in 'ModelInference'. <br>
*std::string proc_name*: Process name used in 'ModelInference'. This is an optional parameter, needed  just when you want the model to be executed in a separate process. <br>
*std::string share_memory_name*: Share memory name used in 'CreateShareMemory'. This is an optional parameter, use it with 'proc_name' together. <br>
*std::vector<uint8_t*>& inputBuffers*: All input data required for the model. <br>
*std::vector<size_t>& inputSize*: The size of input data in 'inputBuffers'. This is an optional parameter, use it with 'proc_name' together. <br>
*std::vector<uint8_t*>& outputBuffers*: Used to save all the output data of the model. <br>
*std::vector<size_t>& outputSize*: The size of output data in 'outputBuffers'. <br>

##### bool LibAppBuilder::ModelDestroy(...) <br>
*std::string model_name*: Model name used in 'ModelInference'. <br>
*std::string proc_name*: Process name used in 'ModelInference'. This is an optional parameter, needed just when you want the model to be executed in a separate process. <br>

##### bool LibAppBuilder::CreateShareMemory(...) <br>
*std::string share_memory_name*: Share memory name. This share memory will be used to store model input & output data. <br>
*size_t share_memory_size*: The one with the larger memory size of the model input and output data. For example: total size of model input data size is 10M, out put data size is 16M, we can set 'share_memory_size' to 16M. <br>

##### bool LibAppBuilder::DeleteShareMemory(...) <br>
*std::string share_memory_name*: Share memory name. <br>

##### Helper function for printing log: <br>
bool SetLogLevel(int32_t log_level) <br>
void QNN_ERR(const char* fmt, ...) <br>
void QNN_WAR(const char* fmt, ...) <br>
void QNN_INF(const char* fmt, ...) <br>
void QNN_VEB(const char* fmt, ...) <br>
void QNN_DBG(const char* fmt, ...) <br>

- log_level: 
```
    QNN_LOG_LEVEL_ERROR   = 1 
    QNN_LOG_LEVEL_WARN    = 2 
    QNN_LOG_LEVEL_INFO    = 3 
    QNN_LOG_LEVEL_VERBOSE = 4 
    QNN_LOG_LEVEL_DEBUG   = 5 
```

## Sample Code(C++: load model to local process.)

```
#include "LibAppBuilder.hpp"

LibAppBuilder libAppBuilder;
SetLogLevel(2);
std::vector<uint8_t*> inputBuffers;
std::vector<uint8_t*> outputBuffers;
std::vector<size_t> outputSize;

libAppBuilder.ModelInitialize(model_name, model_path, backend_lib_path, system_lib_path);

// Fill the inputBuffers before inference.
... 

libAppBuilder.ModelInference(model_name, inputBuffers, outputBuffers, outputSize);

// Use the data in outputBuffers.
... 

// Free the memory in outputBuffers.
for (int j = 0; j < outputBuffers.size(); j++) {
    free(outputBuffers[j]);
}
outputBuffers.clear();
outputSize.clear();

libAppBuilder.ModelDestroy(model_name);
```
