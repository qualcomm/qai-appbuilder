# GenieAPIService User Guide

<div align="center">
  <h2>Run Large Language Models on Local NPU</h2>
  <p><i>OpenAI Compatible API Service (C++)</i></p>
</div>

---

## Table of Contents

1. [📘 Introduction](#introduction)
2. [⚙️ System Requirements](#system-requirements)
3. [✨ Features](#features)
4. [🚀 Platform Deployment](#platform-deployment)
   - [Windows Platform Deployment](#windows-platform-deployment)
   - [Android Platform Deployment](#android-platform-deployment)
5. [🧠 Model Configuration](#model-configuration)
   - [Configuration File Structure](#configuration-file-structure)
   - [Text Model Deployment](#text-model-deployment)
   - [Multimodal Qwen2.5-VL-3B Model](#qwen25-vl-3b-model)
   - [Multimodal Phi-4 Model](#phi-4-multimodal-model)
6. [🛠️ Service Usage](#service-usage)
7. [📱 Client Usage](#client-usage)
   - [C++ Client](#c-client)
   - [Python Client](#python-client)
   - [Other Language Clients](#other-language-clients)
8. [🔨 Building from Source](#building-from-source)
   - [Source Code and Dependencies Preparation](#source-code-and-dependencies-preparation)
   - [Windows Build](#windows-build)
   - [Android Build](#android-build)
9. [🔧 Other Tools](#other-tools)
   - [encode.exe](#encodeexe)
   - [decode.exe](#decodeexe)
   - [wav.exe](#wavexe)
10. [📡 API Reference](#api-reference)
    - [Chat Completion Endpoint](#1-chat-completion-endpoint)
    - [Model List Endpoint](#2-model-list-endpoint)
    - [Text Splitter Endpoint](#3-text-splitter-endpoint)
    - [Stop Output Endpoint](#4-stop-output-endpoint)
    - [Clear History Endpoint](#5-clear-history-endpoint)
    - [Reload History Endpoint](#6-reload-history-endpoint)
    - [Fetch History Endpoint](#7-fetch-history-endpoint)
    - [Get Model Context Size Endpoint](#8-get-model-context-size-endpoint)
    - [Get Model Performance Info Endpoint](#9-get-model-performance-info-endpoint)
    - [Stop Service Endpoint](#10-stop-service-endpoint)
11. [📄 Advanced Examples](#advanced-examples)
    - [Python Basic Chat Example](#python-basic-chat-example)
    - [Tool Calling Example](#tool-calling-example)
    - [Vision Language Model Example](#vision-language-model-example)
    - [Other Language Examples](#other-language-examples)
12. [❓ FAQ](#faq)
13. [📞 Technical Support](#technical-support)

---

## Introduction

GenieAPIService is an OpenAI-compatible API service developed in C++ that runs on Windows on Snapdragon (WoS), mobile devices, and Linux platforms. This service allows developers to run large language models on the NPU (Neural Processing Unit) or CPU of local devices without relying on cloud services.

### Key Advantages

- **Local Execution**: All inference is completed on local devices, protecting data privacy
- **OpenAI Compatible**: Uses standard OpenAI API format for easy integration
- **Multi-platform Support**: Supports Windows, Android, and Linux platforms
- **High Performance**: Utilizes Qualcomm® AI Runtime SDK for NPU acceleration

---

## System Requirements

### Windows Platform

- **Operating System**: Windows 11 or higher
- **Processor**: Devices supporting Qualcomm Snapdragon
- **Memory**: At least 16GB RAM (32GB or more recommended)
- **Storage**: At least 10GB available space (for model files)
- **Software Dependencies**:
    - Qualcomm® AI Runtime SDK (QAIRT) 2.42.0 or higher (included in package, no additional installation required)
    - [Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-160)

### Android Platform

- **Operating System**: Android 10 or higher
- **Processor**: Qualcomm Snapdragon chip (with NPU support)
- **Memory**: At least 8GB RAM (16GB or more recommended)
- **Storage**: At least 10GB available space
- **Permissions**: Storage access and background running permissions required

### Linux Platform

- **Operating System**: Ubuntu 20.04 or higher
- **Processor**: ARM64
- **Memory**: At least 16GB RAM
- **Storage**: At least 10GB available space

---

## Features

GenieAPIService provides rich features:

### Core Features

- ✅ **CPU & NPU Support**: Supports running LLM on both CPU and NPU
- ✅ **Streaming and Non-streaming Modes**: Supports both streaming output and complete response modes
- ✅ **Model Switching**: Supports switching between different models at runtime
- ✅ **Multimodal Support**: Supports Vision Language Models (VLM)
- ✅ **Custom Models**: Supports user-defined model configurations
- ✅ **Text Splitting**: Built-in text splitting functionality for handling long text inputs
- ✅ **Tool Calling**: Supports Function Calling functionality
- ✅ **Thinking Mode**: Supports enabling/disabling thinking mode
- ✅ **LoRA Support**: Supports LoRA adapters
- ✅ **History Management**: Supports conversation history functionality

### Supported Model Formats

- **BIN Format**: Qualcomm QNN format models (default)
- **MNN Format**: Alibaba MNN framework models (requires compilation-time enablement)
- **GGUF Format**: llama.cpp format models (requires compilation-time enablement)

---

## Platform Deployment

## Windows Platform Deployment

### Step 1: Download Resources

1. **Download GenieAPIService**
    - Visit [GitHub Releases](https://github.com/quic/ai-engine-direct-helper/releases/tag/v2.42.0)
    - Download [GenieAPIService_v2.1.4_QAIRT_v2.42.0_v73.zip](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.42.0/GenieAPIService_v2.1.4_QAIRT_v2.42.0_v73.zip)

2. **Download Model Files**
    - [Download](https://www.aidevhome.com/?id=51) corresponding model files as needed
    - Common models: Qwen2.0-7B, Llama-3.1-8B, Qwen2.5-VL-3B, etc.

### Step 2: Extract and Configure

1. **Extract GenieAPIService**
   ```
   Extract GenieAPIService_v2.1.4_QAIRT_v2.42.0_v73.zip to target directory
   Example: C:\GenieAPIService\
   ```

2. **Configure Model Files**
   ```
   Place model files in the models directory
   Directory structure example:
   C:\GenieAPIService\
   ├── GenieAPIService.exe
   ├── models\
   │   ├── Qwen2.0-7B-SSD\
   │   │   ├── config.json
   │   │   ├── model files...
   │   ├── qwen2.5vl3b\
   │   │   ├── config.json
   │   │   ├── model files...
   ```

### Step 3: Start Service

Open Command Prompt (CMD) or PowerShell, navigate to GenieAPIService directory:

#### Start Text Model Service

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l
```

#### Start Vision Language Model Service

```cmd
GenieAPIService.exe -c models/qwen2.5vl3b/config.json -l
```

#### Common Startup Parameters

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l -d 3 -n 10 -o 1024 -p 8910
```

### Step 4: Verify Service

After successful service startup, you'll see information similar to:

```
GenieAPIService: 2.1.4, Genie Library: 1.14.0
current work dir: C:\GenieAPIService
root dir: C:\GenieAPIService
Loading model...
Model loaded successfully
Server listening on port 8910
```

---

## Android Platform Deployment

### Step 1: Install APK

1. **Download APK**
    - Visit [GitHub Releases](https://github.com/quic/ai-engine-direct-helper/releases/tag/v2.42.0)
    - Download [GenieAPIService.apk](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.42.0/GenieAPIService.apk)

2. **Install APK**
   ```
   adb install GenieAPIService.apk
   ```
   Or install directly on device

### Step 2: Prepare Model Files

1. **Create Model Directory**
   ```
   adb shell mkdir -p /sdcard/GenieModels
   ```

2. **Push Model Files**
   ```
   adb push models/Qwen2.0-7B-SSD /sdcard/GenieModels/
   ```

   Model directory structure:
   ```
   /sdcard/GenieModels/
   ├── Qwen2.0-7B-SSD/
   │   ├── config.json
   │   ├── model files...
   ├── qwen2.5vl3b/
   │   ├── config.json
   │   ├── model files...
   ```

### Step 3: Start Service

1. **Open GenieAPIService Application**
    - Find and open the GenieAPIService application on your device

2. **Start Service**
    - Click the `START SERVICE` button
    - Wait for model loading to complete
    - "Genie API Service IS Running." indicates service has started

3. **Configure Background Running** (Important)
    - Go to device Settings → Battery → Power saving settings → App battery management
    - Find GenieAPIService application
    - Select "Allow background activity"

### Step 4: View Logs

- Click the menu in the upper right corner
- Select "Log Files" → "Log:1"
- View service running logs

### Step 5: Install Client Applications

Recommended client applications:

1. **GenieChat**
    - Source code location: `samples/android/GenieChat`
    - Compile and install using Android Studio
    - Or directly [download](https://www.aidevhome.com/zb_users/upload/2026/01/202601281769601771694706.apk) the compiled apk

2. **GenieFletUI**
    - Source code location: `samples/fletui/GenieFletUI/android`
    - Refer to [Build.md](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/fletui/GenieFletUI/android/BUILD.md) to compile Flet code into apk

---

## Model Configuration

### Configuration File Structure

Each model requires a `config.json` configuration file, [reference examples](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/genie/python/models).

Basic configuration file structure:

The [htp_backend_ext_config.json](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/python/config/htp_backend_ext_config.json) used can be downloaded directly.

```json
{
  "dialog" : {
    "version" : 1,
    "type" : "basic",
    "context" : {
      "version" : 1,
      "size": 4096,
      "n-vocab":   152064,
      "bos-token": 151643,
      "eos-token": 151645,
      "eot-token": 128009
    },
    "sampler" : {
      "version" : 1,
      "seed" : 42,
      "temp" : 1,
      "top-k" : 1,
      "top-p" : 1
    },
    "tokenizer" : {
      "version" : 1,
      "path" : "models\\Qwen2.0-7B\\tokenizer.json"
    },
    "engine" : {
      "version" : 1,
      "n-threads" : 3,
      "backend" : {
        "version" : 1,
        "type" : "QnnHtp",
        "QnnHtp" : {
          "version" : 1,
          "use-mmap" : false,
          "spill-fill-bufsize" : 0,
          "mmap-budget" : 0,
          "poll" :false,
          "pos-id-dim" : 64,
          "cpu-mask" : "0xe0",
          "kv-dim" : 128,
          "rope-theta": 1000000
        },
        "extensions" : "models\\htp_backend_ext_config.json"
      },
      "model" : {
        "version" : 1,
        "type" : "binary",
        "binary" : {
          "version" : 1,
          "ctx-bins" : [
            "models\\Qwen2.0-7B\\model-1.bin",
            "models\\Qwen2.0-7B\\model-2.bin",
            "models\\Qwen2.0-7B\\model-3.bin",
            "models\\Qwen2.0-7B\\model-4.bin",
            "models\\Qwen2.0-7B\\model-5.bin"
          ]
        }
      }
    }
  }
}
```

### Text Model Deployment

Standard directory structure for text models:

```
models/Qwen2.0-7B-SSD/
├── config.json           # Model configuration file
├── prompt.json           # Prompt template
├── tokenizer.json        # Tokenizer
├── model-0.bin           # Model file
└── model-1.bin           # Model file
```

### Qwen2.5-VL-3B Model

Qwen2.5-VL-3B is a multimodal vision language model that supports image understanding and text generation.

#### Model Directory Structure

```
models/qwen2.5vl3b/
├── config.json
├── embedding_weights.raw
├── htp_backend_ext_config.json
├── llm_model-0.bin
├── llm_model-1.bin
├── prompt.json
├── tokenizer.json
├── veg.serialized.bin
└── raw/
    ├── full_attention_mask.raw
    ├── position_ids_cos.raw
    ├── position_ids_sin.raw
    └── window_attention_mask.raw
```

#### Qwen2.5-VL-3B Model Deployment

##### Windows Deployment

1) Place the model directory `qwen2.5vl3b` in the `models\` directory.

2) Start the service from the `samples` directory:

```bat
GenieAPIService.exe -c "models\qwen2.5vl3b\config.json" -l
```

3) Prepare a test image `test.png` in the current directory and run the client:

```bat
GenieAPIClient.exe --prompt "what is the image descript?" --img test.png --stream --model qwen2.5vl3b
```

##### Android Deployment

- Place the model at: `/sdcard/GenieModels/qwen2.5vl3b/`
- Install and open the GenieAPIService APK, click **START SERVICE** to load the model

##### Python Client Example

> GenieAPIService provides an OpenAI-compatible interface. For VLM (Vision Language Models), `messages` should contain `{question, image}`, where `image` is a Base64 string.

The example below supports both local image paths and image URLs (URLs are downloaded first then encoded):

```python
import argparse
import base64
import os
import requests
from openai import OpenAI

IP_ADDR = "127.0.0.1:8910"

def encode_image(image_input: str) -> str:
    if image_input.startswith(("http://", "https://")):
        r = requests.get(image_input, timeout=10)
        r.raise_for_status()
        return base64.b64encode(r.content).decode("utf-8")
    if not os.path.exists(image_input):
        raise FileNotFoundError(f"Local file not found: {image_input}")
    with open(image_input, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

parser = argparse.ArgumentParser()
parser.add_argument("--stream", action="store_true")
parser.add_argument("--prompt", default="Describe this image")
parser.add_argument("--image", required=True, help="Local image path or URL")
args = parser.parse_args()

base64_image = encode_image(args.image)
client = OpenAI(base_url="http://" + IP_ADDR + "/v1", api_key="123")

custom_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": {"question": args.prompt, "image": base64_image}},
]
extra_body = {
    "size": 4096,
    "temp": 1.5,
    "top_k": 13,
    "top_p": 0.6,
    "messages": custom_messages,
}
model_name = "qwen2.5vl3b-8380-2.42"
placeholder_msgs = [{"role": "user", "content": "placeholder"}]

resp = client.chat.completions.create(
    model=model_name,
    messages=placeholder_msgs,
    stream=args.stream,
    extra_body=extra_body,
)

if args.stream:
    for chunk in resp:
        if chunk.choices and chunk.choices[0].delta.content is not None:
            print(chunk.choices[0].delta.content, end="", flush=True)
    print()
else:
    print(resp.choices[0].message.content)
```

### Prompt Template Configuration

The `prompt.json` file defines the model's prompt format:

```json
{
  "prompt_system": "<|im_start|>system\n string <|im_end|>\n",
  "prompt_user": "<|im_start|>user\n string <|im_end|>\n",
  "prompt_assistant": "<|im_start|>assistant\n string <|im_end|>\n",
  "prompt_tool": "<|im_start|>tool\n string <|im_end|>\n",
  "prompt_start": "<|im_start|>assistant\n"
}
```

Different models may use different prompt formats. Please configure the appropriate template according to the model documentation. Reference [templates](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/genie/python/models).

---

## Service Usage

### Starting the Service

Basic startup command:

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l
```

### Complete Parameter List

```
GenieAPIService.exe [OPTIONS]

Required Parameters:
  -c, --config_file <path>    Model configuration file path

Optional Parameters:
  -l, --load_model            Automatically load model at startup
  -p, --port <port>           Service port (default: 8910)
  -d, --loglevel <level>      Log level (1-5)
                              1: Error
                              2: Warning
                              3: Info
                              4: Debug
                              5: Verbose
  -n, --num_response <num>    Number of historical conversation rounds to save (default: 10)
  -o, --min_output_num <num>  Minimum output token count (default: 1024)
  -t, --enable_thinking       Enable thinking mode
  -a, --all_text              Output all text (including tool calling)
  --adapter <name>            LoRA adapter name
  --lora_alpha <value>        LoRA Alpha value (default: 1.0)
```

### Usage Examples

#### Basic Startup

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l
```

#### Custom Port and Log Level

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l -p 9000 -d 4
```

#### Enable Thinking Mode

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l -t
```

#### Using LoRA Adapter

```cmd
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l --adapter my_lora --lora_alpha 0.8
```

### Service Status Check

After starting the service, you can check the status in the following ways:

1. **View Console Output**
   ```
   GenieAPIService: 2.1.4, Genie Library: 1.14.0
   Server listening on port 8910
   Model loaded successfully
   ```

2. **Access Health Check Endpoint**
   ```bash
   curl http://localhost:8910/health
   ```

3. **View Model List**
   ```bash
   curl http://localhost:8910/v1/models
   ```

---

## Client Usage

### C++ Client

GenieAPIClient is a C++ command-line client for interacting with GenieAPIService.

#### Basic Usage

```cmd
GenieAPIClient.exe --prompt "Hello, please introduce yourself" --stream
```

#### Complete Parameter List

```
GenieAPIClient.exe [OPTIONS]

Required Parameters:
  --prompt <text>             Input prompt

Optional Parameters:
  --stream                    Enable streaming output
  --model <name>              Specify model name
  --img <path>                Image file path (for multimodal models)
  --host <address>            Server address (default: 127.0.0.1)
  --port <port>               Server port (default: 8910)
  --temperature <value>       Temperature parameter (default: 0.7)
  --top_p <value>             Top-p parameter (default: 0.9)
  --top_k <value>             Top-k parameter (default: 40)
  --max_tokens <num>          Maximum output token count (default: 2048)
```

#### C++ Client Usage
[Reference code](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/c%2B%2B/Service/examples/GenieAPIClient/GenieAPIClient.cpp)<br>
##### Text Chat

```cmd
GenieAPIClient.exe --prompt "Explain the principles of quantum computing" --stream
```

##### Multimodal Chat

```cmd
GenieAPIClient.exe --prompt "What's in this image?" --img test.png --stream --model qwen2.5vl3b
```

##### Custom Parameters

```cmd
GenieAPIClient.exe --prompt "Write a poem" --stream --temperature 0.9 --max_tokens 500
```

### Python Client Usage
[Reference code](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/c%2B%2B/Service/examples/GenieAPIClient/GenieAPIClient.py)<br>
Python client uses OpenAI SDK, providing more flexible integration.

#### Install Dependencies

```bash
pip install openai
```

#### Basic Example

```python
from openai import OpenAI

# Create client
client = OpenAI(
    base_url="http://127.0.0.1:8910/v1",
    api_key="123"  # Any string will work
)

# Send request
response = client.chat.completions.create(
    model="Qwen2.0-7B",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello, please introduce yourself"}
    ],
    stream=True
)

# Process streaming response
for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

#### Non-streaming Mode

```python
response = client.chat.completions.create(
    model="Qwen2.0-7B",
    messages=[
        {"role": "user", "content": "What is artificial intelligence?"}
    ],
    stream=False
)

print(response.choices[0].message.content)
```

#### Custom Parameters

```python
response = client.chat.completions.create(
    model="Qwen2.0-7B",
    messages=[
        {"role": "user", "content": "Write a poem about spring"}
    ],
    temperature=0.9,
    top_p=0.95,
    max_tokens=500,
    stream=True
)
```

### Other Language Clients

GenieAPIService is compatible with OpenAI API, so you can use any client library that supports OpenAI API.

#### JavaScript/Node.js

```javascript
const OpenAI = require('openai');

const client = new OpenAI({
    baseURL: 'http://127.0.0.1:8910/v1',
    apiKey: '123'
});

async function chat() {
    const response = await client.chat.completions.create({
        model: 'Qwen2.0-7B',
        messages: [
            { role: 'user', content: 'Hello' }
        ],
        stream: true
    });

    for await (const chunk of response) {
        if (chunk.choices[0]?.delta?.content) {
            process.stdout.write(chunk.choices[0].delta.content);
        }
    }
}

chat();
```

#### Java

```java
import com.theokanning.openai.OpenAiService;
import com.theokanning.openai.completion.chat.*;

public class GenieClient {
    public static void main(String[] args) {
        OpenAiService service = new OpenAiService("123", Duration.ofSeconds(30));
        service.setBaseUrl("http://127.0.0.1:8910/v1");

        ChatCompletionRequest request = ChatCompletionRequest.builder()
            .model("Qwen2.0-7B")
            .messages(Arrays.asList(
                new ChatMessage("user", "Hello")
            ))
            .build();

        service.createChatCompletion(request)
            .getChoices()
            .forEach(choice -> System.out.println(choice.getMessage().getContent()));
    }
}
```

#### Go

```go
package main

import (
    "context"
    "fmt"
    openai "github.com/sashabaranov/go-openai"
)

func main() {
    config := openai.DefaultConfig("123")
    config.BaseURL = "http://127.0.0.1:8910/v1"
    client := openai.NewClientWithConfig(config)

    resp, err := client.CreateChatCompletion(
        context.Background(),
        openai.ChatCompletionRequest{
            Model: "Qwen2.0-7B",
            Messages: []openai.ChatCompletionMessage{
                {
                    Role:    openai.ChatMessageRoleUser,
                    Content: "Hello",
                },
            },
        },
    )

    if err != nil {
        fmt.Printf("Error: %v\n", err)
        return
    }

    fmt.Println(resp.Choices[0].Message.Content)
}
```
---

## Building from Source

> This section is for developers who need to compile the C++ Service/Client (Windows/Android/Linux) themselves. If you only want to experience or integrate the API, it's recommended to use the [Release package](https://github.com/quic/ai-engine-direct-helper/releases) directly.

### Source Code and Dependencies Preparation

1. **Clone Repository (with submodules)**

```bash
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```

2. **VLM Extra Dependency (stb)**

Vision Language Models (VLM) depend on `stb`, please clone it in the following directory:

```bash
cd samples\genie\c++\External
git clone https://github.com/nothings/stb.git
```

### Windows Build

#### Environment Preparation

- Qualcomm® AI Runtime SDK (QAIRT)
- CMake
- Visual Studio Build Tools 2022 (clang, v143)
- Ninja

> It's recommended to use **Command Prompt (CMD)** rather than PowerShell to execute build commands.

#### Set QAIRT SDK Path

After installing QAIRT, it's typically located at `C:\Qualcomm\AIStack\QAIRT\2.42.0.251225`. You can set an environment variable:

```bat
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.42.0.251225
```

#### Build Commands (ARM64 Example)

```bat
cd samples\genie\c++\Service
mkdir build && cd build
cmake -S .. -B . -A ARM64 ..
cmake --build . --config Release --parallel 4
```

After building, the release directory is typically located at: `Service\GenieSerivce_v2.1.4`.

#### Optional Build Flags (Model Format Support)

GenieService supports Qualcomm QNN BIN models by default. For additional support:

- `USE_MNN=ON`: Support MNN format models
- `USE_GGUF=ON`: Support GGUF (llama.cpp) format models

Append `-D<OPTION>=ON` to the CMake command to enable, for example:

```bat
cmake -S .. -B . -A ARM64 -DUSE_GGUF=ON -DUSE_MNN=ON
```

### Android Build

#### Environment Preparation

- Qualcomm® AI Runtime SDK (QAIRT)
- Android NDK (example: r26d)
- Android Studio (for building APK)

#### Set Environment Variables and Paths (Building Android from Windows Host)

```bat
cd ai-engine-direct-helper\samples\genie\c++\Service
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.42.0.251225
set PATH=%PATH%;C:\Programs\android-ndk-r26d\toolchains\llvm\prebuilt\windows-x86_64\bin
Set NDK_ROOT=C:/Programs/android-ndk-r26d/
Set ANDROID_NDK_ROOT=%NDK_ROOT%
```

#### Build libappbuilder First

Before building for Android, you need to compile `libappbuilder` first (see the project root [BUILD](https://github.com/quic/ai-engine-direct-helper/blob/main/BUILD.md) documentation), and place the generated `libappbuilder.so` at:

`ai-engine-direct-helper\samples\genie\c++\Service`

#### Build GenieAPIService (Android)

```bat
"C:\Programs\android-ndk-r26d\prebuilt\windows-x86_64\bin\make.exe" android -j4
```

After building, copy the dependent `.so` files to `libs/arm64-v8a`:

```bat
copy "%QNN_SDK_ROOT%lib\aarch64-android\*.so" "libs\arm64-v8a" /Y
copy "obj\local\arm64-v8a\*.so" "libs\arm64-v8a" /Y
```

#### Build Android App (Android Studio)

1. Open with Android Studio: `ai-engine-direct-helper\samples\genie\c++\Android`
2. Menu **Build** → **Generate Signed App Bundle / APK** → Select **APK**
3. Select signing key and complete the build
4. Get `app-release.apk` in the `...\Android\app\release` directory
5. Install: `adb install app-release.apk`

---

## Other Tools

These Windows tools are released together with [Releases](https://github.com/quic/ai-engine-direct-helper/releases/tag/v2.42.0)

### encode.exe

It can help you encode images or any files into base64 format files.

**Example**: Encode cat.png to base64 format data and write to cat.txt.

```cmd
encode.exe cat.png cat.txt
```

### decode.exe

It can help you decode base64 encoded files to binary files.

**Example**: Decode base64 format cat.txt to binary and write to cat.png.

```cmd
decode.exe cat.txt cat.png
```

### wav.exe

You may need to input `.wav` format audio files to the `omini` model. `wav.exe` can help record your voice!
We use some additional algorithms and techniques to enhance your voice intensity.

**Example**: After entering the command, press and hold the [Space] key to record...

```cmd
wav.exe test.wav
```

---

## API Reference

GenieAPIService provides a complete set of RESTful API endpoints, compatible with OpenAI API standards.

### 1. Chat Completion Endpoint

**Endpoint**: `POST /v1/chat/completions`

**Description**: Send chat messages and get model responses.

#### Request Parameters

```json
{
  "model": "Qwen2.0-7B",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello"}
  ],
  "stream": true,
  "temperature": 0.7,
  "top_p": 0.9,
  "top_k": 40,
  "max_tokens": 2048,
  "stop": ["</s>"],
  "presence_penalty": 0.0,
  "frequency_penalty": 0.0
}
```

#### Parameter Descriptions

| Parameter | Type | Required | Description |
|------|------|------|------|
| model | string | Yes | Model name |
| messages | array | Yes | Message list |
| stream | boolean | No | Whether to stream output (default: false) |
| temperature | float | No | Temperature parameter (0.0-2.0, default: 0.7) |
| top_p | float | No | Top-p sampling (0.0-1.0, default: 0.9) |
| top_k | integer | No | Top-k sampling (default: 40) |
| max_tokens | integer | No | Maximum output token count (default: 2048) |
| stop | array | No | Stop sequences |
| presence_penalty | float | No | Presence penalty (-2.0-2.0, default: 0.0) |
| frequency_penalty | float | No | Frequency penalty (-2.0-2.0, default: 0.0) |

#### Response Example (Non-streaming)

```json
{
  "id": "chatcmpl-123",
  "object": "chat.completion",
  "created": 1677652288,
  "model": "Qwen2.0-7B",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! I'm an AI assistant, happy to serve you."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 15,
    "total_tokens": 25
  }
}
```

#### Response Example (Streaming)

```
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"Qwen2.0-7B","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"Qwen2.0-7B","choices":[{"index":0,"delta":{"content":"!"},"finish_reason":null}]}

data: [DONE]
```

#### cURL Example

```bash
curl -X POST http://localhost:8910/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen2.0-7B",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'
```

### 2. Model List Endpoint

**Endpoint**: `GET /v1/models`

**Description**: Get list of available models.

#### Request Example

```bash
curl http://localhost:8910/v1/models
```

#### Response Example

```json
{
  "object": "list",
  "data": [
    {
      "id": "Qwen2.0-7B",
      "object": "model",
      "created": 1677652288,
      "owned_by": "genie",
      "permission": [],
      "root": "Qwen2.0-7B",
      "parent": null
    }
  ]
}
```

### 3. Text Splitter Endpoint

**Endpoint**: `POST /v1/textsplitter`

**Description**: Split text according to specified `separators` priority.

#### Call Example

```python
import argparse
from openai import OpenAI
import requests

parser = argparse.ArgumentParser()
parser.add_argument("--model", default="Qwen2.0-7B-SSD", type=str)  
args = parser.parse_args()

url = "http://127.0.0.1:8910/v1/textsplitter"
text = ""   # Please enter the text to be split.
separators = ["\n\n", "\n", "。", "！", "？", "，", ".", "?", "!", ",", " ", ""]
body = {"text": text, "max_length": 128, "separators": separators}
response = requests.post(url, json=body)
result = response.json()
result = result["content"]
print("result length:", len(result))
count = 0
for item in result:
    count += 1
    print("No.", count)
    print("text:", item["text"])
    print("length: Tokens", item["length"], "string", len(item["text"]))
    print()
```

### 4. Stop Output Endpoint

**Endpoint**: `POST /v1/stop`

**Description**: Stop the current generation in progress.

#### Request Example

```python
import requests
url = "http://127.0.0.1:8910/stop"
params = {"text": "stop"}  
response = requests.post(url, json=params)
```

### 5. Clear History Endpoint

**Endpoint**: `POST /v1/clear`

**Description**: Clear conversation history.

#### Request Example

```python
import requests

url = "http://127.0.0.1:8910/clear"
params = {"text": "clear"}
response = requests.post(url, json=params)
```

### 6. Reload History Endpoint

**Endpoint**: `POST /v1/reload`

**Description**: Reload history from file.

#### Call Example

```python
import requests
url = "http://127.0.0.1:8910/reload"
history_data = {
    "action": "import_history",
    "history": [
        {"role": "user", "content": ""},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": ""},
    ]
}

response = requests.post(url, json=history_data)
```

### 7. Fetch History Endpoint

**Endpoint**: `GET /v1/fetch`

**Description**: Get current conversation history.

#### Request Example

```python
import requests
BASE_URL = "http://127.0.0.1:8910/fetch"
response = requests.post(BASE_URL)
print(response.text)
return response
```

### 8. Get Model Context Size Endpoint

**Endpoint**: `GET /v1/contextsize`

**Description**: Get the context size of the current model.

#### Request Example

```python
url = "http://127.0.0.1:8910/contextsize"
params = {"modelName": model_name}  #Llama2.0-7B-SSD
response = requests.post(url, json=params)
if response.status_code == 200:
    result = response.json()
    print("context size:",result["contextsize"])
```

### 9. Get Model Performance Info Endpoint

**Endpoint**: `GET /v1/performance`

**Description**: Get model inference performance information.

#### Request Example

```python
import requests
BASE_URL = "http://127.0.0.1:8910/profile"
response = requests.get(BASE_URL)
return response
```

### 10. Stop Service Endpoint

**Endpoint**: `POST /v1/shutdown`

**Description**: Stop GenieAPIService service.

#### Request Example
```bash
import requests
BASE_URL = "http://127.0.0.1:8910/shutdown"
response = requests.get(BASE_URL)
return response
```
---

## Advanced Examples

### Python Basic Chat Example

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8910/v1",
    api_key="123"
)

# Streaming chat
response = client.chat.completions.create(
    model="Qwen2.0-7B",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Introduce quantum computing"}
    ],
    stream=True,
    temperature=0.7,
    max_tokens=2048
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

### Tool Calling Example

GenieAPIService supports Function Calling, allowing the model to call external tools.

```python
from openai import OpenAI
import json

client = OpenAI(
    base_url="http://127.0.0.1:8910/v1",
    api_key="123"
)

# Define tools
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather information for a specified city",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "City name, e.g., Beijing, Shanghai"
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "Temperature unit"
                    }
                },
                "required": ["city"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Perform mathematical calculations",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Mathematical expression, e.g., 2+2, 10*5"
                    }
                },
                "required": ["expression"]
            }
        }
    }
]

# Send request
messages = [
    {"role": "system", "content": "You are a helpful assistant with access to tools."},
    {"role": "user", "content": "What's the weather like in Beijing today?"}
]

response = client.chat.completions.create(
    model="Qwen2.0-7B",
    messages=messages,
    tools=tools,
    tool_choice="auto"
)

# Process tool calls
message = response.choices[0].message
if message.tool_calls:
    for tool_call in message.tool_calls:
        function_name = tool_call.function.name
        function_args = json.loads(tool_call.function.arguments)
        
        print(f"Calling tool: {function_name}")
        print(f"Parameters: {function_args}")
        
        # Simulate tool execution
        if function_name == "get_weather":
            result = {
                "city": function_args["city"],
                "temperature": 22,
                "condition": "Sunny",
                "humidity": 45
            }
        
        # Add tool result to messages
        messages.append(message)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(result, ensure_ascii=False)
        })
    
    # Get final response
    final_response = client.chat.completions.create(
        model="Qwen2.0-7B",
        messages=messages
    )
    
    print(f"\nFinal response: {final_response.choices[0].message.content}")
else:
    print(message.content)
```

### Vision Language Model Example

```python
from openai import OpenAI
import base64
import os

client = OpenAI(
    base_url="http://127.0.0.1:8910/v1",
    api_key="123"
)

def encode_image(image_path):
    """Encode image to base64"""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

# Prepare image
image_path = "test.png"
base64_image = encode_image(image_path)

# Send request (use extra_body to pass custom message format)
custom_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {
        "role": "user",
        "content": {
            "question": "Please describe the content of this image in detail",
            "image": base64_image
        }
    }
]

response = client.chat.completions.create(
    model="qwen2.5vl3b",
    messages=[{"role": "user", "content": "placeholder"}],  # Placeholder
    stream=True,
    extra_body={
        "messages": custom_messages,
        "size": 4096,
        "temp": 1.5,
        "top_k": 13,
        "top_p": 0.6
    }
)

# Process streaming response
print("Model response:")
for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

### Other Language Examples

#### JavaScript/TypeScript

```javascript
import OpenAI from 'openai';

const client = new OpenAI({
    baseURL: 'http://127.0.0.1:8910/v1',
    apiKey: '123'
});

async function chat() {
    const response = await client.chat.completions.create({
        model: 'Qwen2.0-7B',
        messages: [
            { role: 'system', content: 'You are a helpful assistant.' },
            { role: 'user', content: 'Hello' }
        ],
        stream: true
    });

    for await (const chunk of response) {
        if (chunk.choices[0]?.delta?.content) {
            process.stdout.write(chunk.choices[0].delta.content);
        }
    }
    console.log();
}

chat();
```

#### C#

```csharp
using OpenAI;
using OpenAI.Chat;

var client = new OpenAIClient("123", new OpenAIClientOptions
{
    Endpoint = new Uri("http://127.0.0.1:8910/v1")
});

var chatClient = client.GetChatClient("Qwen2.0-7B");

var response = await chatClient.CompleteChatAsync(
    new ChatMessage[]
    {
        new SystemChatMessage("You are a helpful assistant."),
        new UserChatMessage("Hello")
    }
);

Console.WriteLine(response.Value.Content[0].Text);
```

---

## FAQ

### 1. Service Startup Failure

**Problem**: Running `GenieAPIService.exe` prompts that DLL files cannot be found.

**Solution**:
- Ensure [Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-160) is installed
- Check if QAIRT SDK is correctly installed
- Confirm all dependent `.dll` files are in the same directory

### 2. Model Loading Failure

**Problem**: After service startup, it prompts "Failed to load model".

**Solution**:
- Check if `config.json` file path is correct
- Confirm model files are complete and not corrupted
- Check if system memory is sufficient (at least 16GB)
- View log files for detailed error information

### 3. NPU Unavailable

**Problem**: Service runs on CPU instead of NPU.

**Solution**:
- Confirm device supports Qualcomm Snapdragon NPU
- Check if QAIRT SDK version is correct
- Set `"device": "npu"` in `config.json`
- Update device drivers

### 4. Slow Inference Speed

**Problem**: Model inference speed is slower than expected.

**Solution**:
- Confirm model is running on NPU instead of CPU
- Check system resource usage
- Try reducing `context_size` or `max_tokens`
- Close unnecessary background applications

### 5. Streaming Output Not Working

**Problem**: Set `stream=true` but no streaming output.

**Solution**:
- Confirm client correctly handles SSE (Server-Sent Events)
- Check network connection and firewall settings
- Try using non-streaming mode to test if service is normal

### 6. Multimodal Model Cannot Recognize Images

**Problem**: Model doesn't respond or errors after sending images.

**Solution**:
- Confirm image is correctly encoded to base64
- Check if image format is supported (PNG, JPEG)
- Confirm using a multimodal model (e.g., qwen2.5vl3b)
- Check if message format is correct (requires `{question, image}` format)

### 7. Android Service Automatically Stops

**Problem**: Service automatically stops after running for a while on Android.

**Solution**:
- Allow app to run in background in settings
- Turn off battery optimization
- Add app to whitelist
- Ensure sufficient storage space

### 8. Tool Calling Not Working

**Problem**: Sent `tools` parameter but model doesn't call tools.

**Solution**:
- Confirm model supports Function Calling
- Check if tool definition format is correct
- Try setting `tool_choice="auto"` or specify tool name
- Check if model requires specific prompt format

### 9. History Lost

**Problem**: Conversation history is lost after restarting service.

**Solution**:
- Use `-n` parameter to set number of history rounds to save
- Regularly call `/v1/history` endpoint to backup history

### 10. Port Already in Use

**Problem**: Starting service prompts port 8910 is already in use.

**Solution**:
- Use `-p` parameter to specify another port
- Check if other GenieAPIService instances are running
- Use `netstat -ano | findstr 8910` to find process occupying the port

### 11. LoRA Adapter Loading Failure

**Problem**: Service errors after using `--adapter` parameter.

**Solution**:
- Confirm LoRA file path is correct
- Check if LoRA file is compatible with base model
- Try adjusting `--lora_alpha` parameter
- View logs for detailed error information

### 12. Output Text Garbled

**Problem**: Model output contains garbled text or special characters.

**Solution**:
- Confirm terminal supports UTF-8 encoding
- Check if tokenizer file is correct
- Try testing with different clients
- Update model files to latest version

---

## Technical Support

### Getting Help

If you encounter problems using GenieAPIService, you can get help through the following ways:

1. **View Documentation**
   - [GitHub Repository](https://github.com/quic/ai-engine-direct-helper)
   - [API Documentation](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/c%2B%2B/docs/API.md)
   - [Example Code](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/genie/c%2B%2B/Service/examples/GenieAPIClient)

2. **Submit Issues**
   - [GitHub Issues](https://github.com/quic/ai-engine-direct-helper/issues)

### Reporting Bugs

When reporting bugs, please provide the following information:

1. **Environment Information**
   ```
   - Operating System: Windows 11 / Android 13 / Ubuntu 22.04
   - Device Model: Surface Pro X / Samsung Galaxy S23
   - GenieAPIService Version: 2.1.4
   - QAIRT Version: 2.42.0
   ```

2. **Problem Description**
   - Detailed description of the problem
   - Expected behavior and actual behavior
   - Steps to reproduce

3. **Log Information**
   - Startup logs
   - Error logs
   - Related API requests and responses

4. **Configuration Files**
   - `config.json` content
   - Startup parameters

### Contributing Code

Contributions to GenieAPIService are welcome:

1. Fork the repository
2. Create a feature branch
3. Commit changes
4. Push to branch
5. Create Pull Request

### License

GenieAPIService uses the BSD-3-Clause license. See [LICENSE](https://github.com/quic/ai-engine-direct-helper/blob/main/LICENSE) file for details.

### Contact Information

- **Project Homepage**: https://github.com/quic/ai-engine-direct-helper
- **Issue Feedback**: https://github.com/quic/ai-engine-direct-helper/issues

---

<div align="center">
  <p>Thank you for using GenieAPIService!</p>
  <p>If you have any questions or suggestions, feel free to communicate with us on GitHub.</p>
</div>
