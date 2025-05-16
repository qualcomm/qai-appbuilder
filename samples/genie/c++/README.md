# README

## Introduction 
This sample helps developers use C++ to build Genie based Open AI compatibility API service on Windows on Snapdragon (WoS), Mobile, Linux platforms.

# GenieAPIService
Genie OpenAI Compatible API Service.

This is an OpenAI compatible API service that can be used to access the Genie AI model.
This service can be used on multiple platforms such as Android, Windows, Linux, etc.

# Source code
## Service:
  The code under this folder is C++ implementation of the service. It can be compiled to Windows, Android and Linux target.

## Android:
  The code under this folder is Android app which can be used to launch the service in Android device.

## Build Service from source code:

### Build GenieAPIServer for WoS:<br>
Install Qualcomm® AI Runtime SDK, CMake, Visual Studio etc, before you compile this service.<br>
```
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.34.0.250424\
cd samples\genie\c++\Service
mkdir build && cd build
cmake -S .. -B . -A ARM64
cmake --build . --config Release
```

### Build GenieAPIServer for Android: <br>
Install Qualcomm® AI Runtime SDK, Android NDK etc, before you compile this service.<br>
```
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.34.0.250424\
set PATH=%PATH%;C:\Programs\android-ndk-r26d\toolchains\llvm\prebuilt\windows-x86_64\bin
Set NDK_ROOT=C:/Programs/android-ndk-r26d/
Set ANDROID_NDK_ROOT=%NDK_ROOT%

"C:\Programs\android-ndk-r26d\prebuilt\windows-x86_64\bin\make.exe" android
```

### Run the service on WoS: <br>
1. [Setup LLM models](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/genie/python#step-3-download-models-and-tokenizer-files) first before running the service. <br>
2. Download [Pre-build app](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/GenieAPIService_2.34.zip) and copy "GenieAPIService" folder to path "ai-engine-direct-helper\samples".<br>
3. Run the commands below to launch the Service.

```
cd ai-engine-direct-helper\samples
GenieAPIService\GenieAPIService.exe -c "genie\python\models\IBM-Granite-v3.1-8B\config.json"  -l
```

### Run the service on Mobile(Snapdragon® 8 Elite Mobile device): <br>
1. Copy GenieModels folder to the root folder of mobile sdcard.<br>
2. Copy your LLM model & tokenizer.json to "/sdcard/GenieModels/qwen2.0_7b"<br>
3. Modify the config file "/sdcard/GenieModels/qwen2.0_7b/config.json" if necessary.<br>
4. Install the GenieAPIService.apk to mobile and start it.<br>

## Client Usage:
  The service can be access through http://ip:8910/, it's compatible with OpenAI API.
  Here is a Python client sample:
```
import argparse
from openai import OpenAI

HOST="localhost"
PORT="8910"

parser = argparse.ArgumentParser()
parser.add_argument("--stream", action="store_true")
parser.add_argument("--prompt", default="Hello", type=str)
args = parser.parse_args()

client = OpenAI(base_url="http://" + HOST + ":" + PORT + "/v1", api_key="123")

model_lst = client.models.list()
print(model_lst)

messages = [{"role": "system", "content": "You are a math teacher who teaches algebra."}, {"role": "user", "content": args.prompt}]
extra_body = {"size": 4096, "temp": 1.5, "top_k": 13, "top_p": 0.6}

model_name = "qwen2.0_7b"

if args.stream:
    response = client.chat.completions.create(model=model_name, stream=True, messages=messages, extra_body=extra_body)

    for chunk in response:
        content = chunk.choices[0].delta.content
        if content is not None:
            print(content, end="", flush=True)
else:
    response = client.chat.completions.create(model=model_name, messages=messages, extra_body=extra_body)
    print(response.choices[0].message.content)
```
