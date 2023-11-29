# User Guide

## Environment Setup

### 1. Libraries from QNN SDK:

<b>We need the below libraries from QNN SDK for using QNNHelper on Snapdragon X Elite device:</b> <br>

```
C:\Qualcomm\AIStack\QNN\2.16.0.231029\lib\aarch64-windows-msvc\QnnHtp.dll
C:\Qualcomm\AIStack\QNN\2.16.0.231029\lib\aarch64-windows-msvc\QnnHtpNetRunExtensions.dll
C:\Qualcomm\AIStack\QNN\2.16.0.231029\lib\aarch64-windows-msvc\QnnHtpPrepare.dll
C:\Qualcomm\AIStack\QNN\2.16.0.231029\lib\aarch64-windows-msvc\QnnSystem.dll
C:\Qualcomm\AIStack\QNN\2.16.0.231029\lib\aarch64-windows-msvc\QnnHtpV73Stub.dll
C:\Qualcomm\AIStack\QNN\2.16.0.231029\lib\hexagon-v73\unsigned\libQnnHtpV73Skel.so
```
We can copy these libraries to one folder, for example: ```C:\<Project Name>\QNN_binaries\``` <br>

### 2. Python and common python extensions: 
Get ARM64 version 'python-3.10.5-arm64.exe' from below link and install it:
https://github.com/RockLakeGrass/Windows-on-ARM64-Toolchain/tree/main/Python/installer

Get common python extensions such as PyQt, NumPy, OpenCV, Pillow here:
https://github.com/RockLakeGrass/Windows-on-ARM64-Toolchain/tree/main/Python/packages

### 3. MSVC library: 
You need 'msvcp140.dll' from MSFT too.


## API from 'libQNNHelper.dll' for C++ projects.

###bool LibQNNHelper::ModelInitialize(...) <br>
*std::string model_name*: Model name such as "unet", "text_encoder", "controlnet_canny". Model name must be unique for different model files. <br>
*std::string proc_name*: This is an optional parameter, needed just when you want the model to be executed in a separate process. If use process name, this model will be loaded in 'SvcQNNHelper' service process. One service process can load many models. There can be many service processes. <br>
*std::string model_path*: The path of model. <br>
*std::string backend_lib_path*: The path of 'QnnHtp.dll' <br>
*std::string system_lib_path*: The path of 'QnnSystem.dll' <br>
*std::string backend_ext_lib_path*: The path of 'QnnHtpNetRunExtensions.dll' <br>
*std::string backend_ext_config_path*: The path of 'htp_backend_ext_config.json' <br>

Sample 'htp_backend_ext_config.json' for Snapdragon X Elite:

```
{
  "graphs": {
    "O": 3,
    "fp16_relaxed_precision": 0,
    "vtcm_mb": 8
  },
  "devices": [
    {
      "pd_session": "unsigned"
    }
  ]
}
```

### bool LibQNNHelper::ModelInference(...) <br>
*std::string model_name*: Model name used in 'ModelInference'. <br>
*std::string proc_name*: Process name used in 'ModelInference'. This is an optional parameter, needed  just when you want the model to be executed in a separate process. <br>
*std::string share_memory_name*: Share memory name used in 'CreateShareMemory'. This is an optional parameter, use it with 'proc_name' together. <br>
*std::vector<uint8_t*>& inputBuffers*: All input data required for the model. <br>
*std::vector<size_t>& inputSize*: The size of input data in 'inputBuffers'. This is an optional parameter, use it with 'proc_name' together. <br>
*std::vector<uint8_t*>& outputBuffers*: Used to save all the output data of the model. <br>
*std::vector<size_t>& outputSize*: The size of output data in 'outputBuffers'. <br>

### bool LibQNNHelper::ModelDestroy(...) <br>
*std::string model_name*: Model name used in 'ModelInference'. <br>
*std::string proc_name*: Process name used in 'ModelInference'. This is an optional parameter, needed just when you want the model to be executed in a separate process. <br>

### bool LibQNNHelper::CreateShareMemory(...) <br>
*std::string share_memory_name*: Share memory name. This share memory will be used to store model input & output data. <br>
*size_t share_memory_size*: The one with the larger memory size of the model input and output data. For example: total size of model input data size is 10M, out put data size is 16M, we can set 'share_memory_size' to 16M. <br>

### bool LibQNNHelper::DeleteShareMemory(...) <br>
*std::string share_memory_name*: Share memory name. <br>

### Helper function for printing log: <br>
bool SetLogLevel(int32_t log_level) <br>
void QNN_ERR(const char* fmt, ...) <br>
void QNN_WAR(const char* fmt, ...) <br>
void QNN_INF(const char* fmt, ...) <br>
void QNN_VEB(const char* fmt, ...) <br>
void QNN_DBG(const char* fmt, ...) <br>

log_level: <br>
    QNN_LOG_LEVEL_ERROR   = 1 <br>
    QNN_LOG_LEVEL_WARN    = 2 <br>
    QNN_LOG_LEVEL_INFO    = 3 <br>
    QNN_LOG_LEVEL_VERBOSE = 4 <br>
    QNN_LOG_LEVEL_DEBUG   = 5 <br>

## Sample Code(C++: load model to local process.)

```
#include "libQNNHelper.hpp"

LibQNNHelper libQNNHelper;
SetLogLevel(2);
std::vector<uint8_t*> inputBuffers;
std::vector<uint8_t*> outputBuffers;
std::vector<size_t> outputSize;

libQNNHelper.ModelInitialize(model_name, model_path, backend_lib_path, system_lib_path, 
                             backend_ext_lib_path, backend_ext_config_path);
... // fill the inputBuffers before inference.
libQNNHelper.ModelInference(model_name, inputBuffers, outputBuffers, outputSize);
... // Use the data in outputBuffers.
... // Free the memory in outputBuffers.
for (int j = 0; j < outputBuffers.size(); j++) {
    free(outputBuffers[j]);
}
outputBuffers.clear();
outputSize.clear();
libQNNHelper.ModelDestroy(model_name);
```

## Sample Code(C++: load model to ‘SvcQNNHelper’ process.)

```
#include "libQNNHelper.hpp"

LibQNNHelper libQNNHelper;
SetLogLevel(2);
std::vector<uint8_t*> inputBuffers;
std::vector<size_t> inputSize;
std::vector<uint8_t*> outputBuffers;
std::vector<size_t> outputSize;

libQNNHelper.CreateShareMemory(model_memory_name, MODEL_SHAREMEM_SIZE);
libQNNHelper.ModelInitialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path, 
                             backend_ext_lib_path, backend_ext_config_path);
... // fill the inputBuffers before inference.
libQNNHelper.ModelInference(model_name, proc_name, model_memory_name, inputBuffers, inputSize, 
                            outputBuffers, outputSize);
... // Use the data in outputBuffers. 
... // The data is in share memory, we don’t need to free it immediately if we still need to use it.
libQNNHelper.ModelDestroy(model_name, proc_name);
libQNNHelper.DeleteShareMemory(model_memory_name);  // Free the memory here.
```

## API from 'QNNHelper.pyd' for Python projects.<br>
Number *model_initialize*(String model_name, String model_path, String backend_lib_path, String system_lib_path, String backend_ext_lib_path, String backend_ext_config_path) <br>
Number *model_initialize*(String model_name, String proc_name, String model_path, String backend_lib_path, String system_lib_path, String backend_ext_lib_path, String backend_ext_config_path) <br>
List<ndarray<float>> *model_inference*(String model_name, List<ndarray<float>> input) <br>
List<ndarray<float>> *model_inference*(String model_name, String proc_name, String share_memory_name, List<ndarray<float>> input) <br>
Number *model_destroy*(String model_name) <br>
Number *model_destroy*(String model_name, String proc_name) <br>
Number *memory_create*(String share_memory_name, Number share_memory_size) <br>
Number *memory_delete*(String share_memory_name) <br>
Number *set_log_level*(Number log_level) <br>

*Parameters: refer to C++ API.

## Sample Code(Python)

```
import qnnhelper

QNN_LOG_LEVEL_WARN = 2
qnnhelper.set_log_level(QNN_LOG_LEVEL_WARN)

if RUN_WITH_MULTI_PROCESSES: 
    qnnhelper.model_initialize(model_unet, model_unet_proc, unet_model, lib_backend, lib_system, 
                               lib_backend_ext, lib_backend_ext_config)
    qnnhelper.model_initialize(model_controlnet, model_unet_proc, controlnet_model, lib_backend, lib_system, 
                               lib_backend_ext, lib_backend_ext_config)

    # 28M for running Unet & ControlNet models in 'SvcQNNHelper' process.
    qnnhelper.memory_create(model_unet_mem_1, 1024 * 1024 * 28)
    qnnhelper.memory_create(model_unet_mem_2, 1024 * 1024 * 28)
else:
    qnnhelper.model_initialize(model_unet, unet_model, lib_backend, lib_system, 
                               lib_backend_ext, lib_backend_ext_config)
    qnnhelper.model_initialize(model_controlnet, controlnet_model, lib_backend, lib_system, 
                               lib_backend_ext, lib_backend_ext_config)

qnnhelper.model_initialize(model_text_encoder, text_encoder_model, lib_backend, lib_system, 
                           lib_backend_ext, lib_backend_ext_config) 
qnnhelper.model_initialize(model_vae_decoder, model_vae_decoder_proc, vae_decoder_model, 
                           lib_backend, lib_system, lib_backend_ext, lib_backend_ext_config)
# 4M for running vae decode model in 'SvcQNNHelper' process.
qnnhelper.memory_create(model_vae_decoder_mem, 1024 * 1024 * 4)
...
uncond_text_embedding = run_text_encoder(uncond_tokens) 
user_text_embedding = run_text_encoder(cond_tokens)

for step in range(user_step):
    conditional_noise_preds = run_controlnet(latent_in, time_embedding, user_text_embedding, canny_image, model_unet_mem_1) 
    conditional_noise_pred = run_unet(latent_in, time_embedding, user_text_embedding, 
                                      conditional_noise_preds, model_unet_mem_1) 

    unconditional_noise_preds = run_controlnet(latent_in, time_embedding, uncond_text_embedding, canny_image, model_unet_mem_2) 
    unconditional_noise_pred = run_unet(latent_in, time_embedding, uncond_text_embedding, 
                                        unconditional_noise_preds, model_unet_mem_2) 

    latent_in = run_scheduler(unconditional_noise_pred, conditional_noise_pred, latent_in, timestep)

output_image = run_vae(latent_in)


qnnhelper.model_destroy(model_text_encoder)

if RUN_WITH_MULTI_PROCESS: 
    qnnhelper.model_destroy(model_unet, model_unet_proc) 
    qnnhelper.model_destroy(model_controlnet, model_controlnet_proc)

    qnnhelper.memory_delete(model_unet_mem_1) 
    qnnhelper.memory_delete(model_unet_mem_2)
else:
    qnnhelper.model_destroy(model_unet) 
    qnnhelper.model_destroy(model_controlnet) 

qnnhelper.model_destroy(model_vae_decoder, model_vae_decoder_proc) 
qnnhelper.memory_delete(model_vae_decoder_mem)


def run_text_encoder(input_data):
    input_datas=[input_data]
    output_data = qnnhelper.model_inference(model_text_encoder, input_datas)[0]
    output_data = output_data.reshape((1, 77, 768)) 
    return output_data

def run_controlnet(input_data_1, input_data_2, input_data_3, canny_image, model_memory):
    input_data_1 = input_data_1.reshape(input_data_1.size) 
    input_data_3 = input_data_3.reshape(input_data_3.size) 
    canny_image = canny_image.reshape(canny_image.size)
    input_datas=[input_data_1, input_data_2, input_data_3, canny_image]

if RUN_WITH_MULTI_PROCESS: 
    output_datas = qnnhelper.model_inference(model_controlnet, 
                                             model_controlnet_proc, model_memory, input_datas) 
else:
    output_datas = qnnhelper.model_inference(model_controlnet, input_datas
    return output_datas

def run_unet(input_data_1, input_data_2, input_data_3, controlnet_outputs, 
             model_memory):
    input_data_1 = input_data_1.reshape(input_data_1.size) 
    input_data_3 = input_data_3.reshape(input_data_3.size) 
    input_datas=[input_data_1, input_data_2, input_data_3] 
    input_datas.extend(controlnet_outputs)
   if RUN_WITH_MULTI_PROCESS: 
        output_data = qnnhelper.model_inference(model_unet, model_unet_proc,
                                                model_memory, input_datas)[0] 
    else:
        output_data = qnnhelper.model_inference(model_unet, input_datas)[0] 

    output_data = output_data.reshape(1, 64, 64, 4) 
    return output_data

def run_vae(input_data):
    input_data = input_data.reshape(input_data.size) 
    input_datas=[input_data] 

    output_data = qnnhelper.model_inference(model_vae_decoder, 
                                            model_vae_decoder_proc, model_vae_decoder_mem, input_datas)[0] 

    return output_data
```

## Multi-Processes
<b>There is no limit to the number of ‘SvcQNNHelper’ processes.</b>

![Multi-Processes](resource/multi-processes.png)

## Share Memory
<b>Minimize memory copies.</b>

![Share Memory](resource/share-memory.png)
