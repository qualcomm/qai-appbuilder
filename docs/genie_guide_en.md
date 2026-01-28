# GenieAPIService User Guide

<div align="center">
  <h2>Run Large Language Models on Local NPU</h2>
  <p><i>OpenAI Compatible API Service (C++)</i></p>
</div>

---

## Table of Contents

1. [Introduction](#introduction)
2. [System Requirements](#system-requirements)
3. [Features](#features)
4. [Windows Platform Deployment](#windows-platform-deployment)
5. [Android Platform Deployment](#android-platform-deployment)
6. [Model Configuration](#model-configuration)
7. [Service Usage](#service-usage)
8. [Client Usage](#client-usage)
9. [API Reference](#api-reference)
10. [Python Client Examples](#python-client-examples)
11. [FAQ](#faq)
12. [Technical Support](#technical-support)

---

## Introduction

GenieAPIService is an OpenAI-compatible API service developed in C++ that runs on Windows on Snapdragon (WoS), mobile devices, and Linux platforms. This service allows developers to run large language models on the NPU (Neural Processing Unit) or CPU of local devices without relying on cloud services.

### Key Advantages

- **Local Execution**: All inference is completed on local devices, protecting data privacy
- **OpenAI Compatible**: Uses standard OpenAI API format for easy integration
- **Multi-Platform Support**: Supports Windows, Android, and Linux platforms
- **High Performance**: Leverages Qualcomm® AI Runtime SDK for NPU acceleration

---

## System Requirements

### Windows Platform

- **Operating System**: Windows 11 or higher
- **Processor**: Devices with Qualcomm Snapdragon support
- **Memory**: At least 16GB RAM (32GB or more recommended)
- **Storage**: At least 10GB available space (for model files)
- **Software Dependencies**:
  - Qualcomm® AI Runtime SDK (QAIRT) 2.42.0 or higher (included in package, no additional installation required)
  - Visual C++ Redistributable

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
- ✅ **Streaming and Non-Streaming Modes**: Supports both streaming output and complete response modes
- ✅ **Model Switching**: Supports switching between different models at runtime
- ✅ **Multimodal Support**: Supports Vision Language Models (VLM)
- ✅ **Custom Models**: Supports user-defined model configurations
- ✅ **Text Splitting**: Built-in text splitting functionality for handling long text inputs
- ✅ **Tool Calling**: Supports Function Calling functionality
- ✅ **Thinking Mode**: Supports enabling/disabling thinking mode
- ✅ **LoRA Support**: Supports LoRA adapters
- ✅ **History**: Supports conversation history functionality

### Supported Model Formats

- **BIN Format**: Qualcomm QNN format models (default)
- **MNN Format**: Alibaba MNN framework models (requires compile-time enablement)
- **GGUF Format**: llama.cpp format models (requires compile-time enablement)

---

## Windows Platform Deployment

### Step 1: Download Resources

1. **Download GenieAPIService**
   - Visit [GitHub Releases](https://github.com/quic/ai-engine-direct-helper/releases/tag/v2.42.0)
   - Download `GenieAPIService_v2.1.3_QAIRT_v2.42.0_v73.zip`

2. **Download Model Files**
   - Download corresponding model files as needed
   - Common models: Qwen2.0-7B, IBM-Granite-v3.1-8B, Qwen2.5-VL-3B, etc.

### Step 2: Extract and Configure

1. **Extract GenieAPIService**
   ```
   Extract GenieAPIService_v2.1.3_QAIRT_v2.42.0_v73.zip to target directory
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

Open Command Prompt (CMD) or PowerShell and navigate to the GenieAPIService directory:

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

Parameter descriptions:
- `-c, --config_file`: Configuration file path (required)
- `-l, --load_model`: Load model at startup
- `-d, --loglevel`: Log level (1:Error, 2:Warning, 3:Info, 4:Debug, 5:Verbose)
- `-n, --num_response`: Number of conversation rounds to save in history
- `-o, --min_output_num`: Minimum output token count
- `-p, --port`: Service port (default 8910)
- `-t, --enable_thinking`: Enable thinking mode
- `-a, --all_text`: Output all text (including tool call text)
- `--adapter`: LoRA adapter name
- `--lora_alpha`: LoRA Alpha value

### Step 4: Verify Service

After successful service startup, you will see information similar to:

```
GenieAPIService: 2.1.3, Genie Library: x.x.x
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
   - Download `GenieAPIService.apk`

2. **Install APK**
   ```bash
   adb install GenieAPIService.apk
   ```
   Or install directly on the device

### Step 2: Prepare Model Files

1. **Create Model Directory**
   ```bash
   adb shell mkdir -p /sdcard/GenieModels
   ```

2. **Push Model Files**
   ```bash
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

1. **Open GenieAPI Application**
   - Find and open the GenieAPI application on your device

2. **Start Service**
   - Click the `START SERVICE` button
   - Wait for model loading to complete
   - "Genie API Service IS Running." indicates the service has started

3. **Configure Background Running** (Important)
   - Go to device Settings → Battery → Power saving settings → App battery management
   - Find the GenieAPI application
   - Select "Allow background activity"

### Step 4: View Logs

- Click the menu in the upper right corner
- Select "Log Files" → "Log:1"
- View service running logs

### Step 5: Install Client Application

Recommended client applications:

1. **GenieChat**
   - Source location: `samples/android/GenieChat`
   - Compile and install using Android Studio

2. **GenieFletUI**
   - Source location: `samples/fletui/GenieFletUI/android`
   - Compile and install using Android Studio

---

## Model Configuration

### Configuration File Structure

Each model requires a `config.json` configuration file, [reference examples](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/genie/python/models).

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

### Vision Language Model Deployment

#### Qwen2.5-VL-3B Model

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

#### Phi-4 Multimodal Model

```
models/phi4mm/
├── config.json
├── embedding_weights_200064x3072.raw
├── prompt.json
├── tokenizer.json
├── veg.serialized.bin
├── llm_model-0.bin
├── llm_model-1.bin
└── raw/
    ├── attention_mask.bin
    └── position_ids.bin
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

---

## Service Usage

### Starting the Service

#### Basic Startup

```bash
# Windows
GenieAPIService.exe -c models/Qwen2.0-7B-SSD/config.json -l

# Linux
./GenieAPIService -c models/Qwen2.0-7B-SSD/config.json -l
```

#### Advanced Configuration Startup

```bash
GenieAPIService.exe \
  -c models/Qwen2.0-7B-SSD/config.json \
  -l \
  -d 3 \
  -n 10 \
  -o 1024 \
  -p 8910 \
  -f service.log
```

### Service Port

Default port: `8910`

Can be modified with the `-p` parameter:
```bash
GenieAPIService.exe -c config.json -l -p 9000
```

### Log Configuration

#### Log Levels

- `1`: Error (errors only)
- `2`: Warning (warnings and above)
- `3`: Info (information and above)
- `4`: Debug (debug and above)
- `5`: Verbose (detailed information)

#### Log File

```bash
# Specify log file
GenieAPIService.exe -c config.json -l -d 3 -f service.log
```

### History Functionality

Enabling history functionality allows the model to remember previous conversations:

```bash
# Save the last 10 conversation rounds
GenieAPIService.exe -c config.json -l -n 10
```

**Notes**:
- History takes up context length
- Input length + history length + output length cannot exceed the model's maximum context length
- History can be cleared or reloaded via API

### Thinking Mode

Some models support thinking mode (such as DeepSeek-R1):

```bash
# Enable thinking mode
GenieAPIService.exe -c config.json -l -t
```

**Recommendation**: When using tool calling functionality, it is recommended to disable thinking mode.

### LoRA Support

Using LoRA adapters:

```bash
GenieAPIService.exe -c config.json -l --adapter my_adapter --lora_alpha 0.5
```

---

## Client Usage

### C++ Client

GenieAPIService provides C++ client examples.

#### Text Model Invocation

```bash
GenieAPIClient.exe \
  --prompt "How to learn programming?" \
  --stream \
  --model Qwen2.0-7B-SSD \
  --ip 127.0.0.1
```

#### Vision Language Model Invocation

```bash
GenieAPIClient.exe \
  --prompt "What does this image describe?" \
  --img test.png \
  --stream \
  --model qwen2.5vl3b \
  --ip 127.0.0.1
```

#### Client Parameter Descriptions

- `--prompt`: User question (required)
- `--system`: System prompt (default: "You are a helpful assistant.")
- `--stream`: Enable streaming output
- `--model`: Model name (required)
- `--ip`: Server IP address (default: 127.0.0.1)
- `--img`: Image path (for VLM models only)

### Python Client

Using OpenAI Python SDK to connect to the service:

```python
from openai import OpenAI

# Create client
client = OpenAI(
    base_url="http://127.0.0.1:8910/v1",
    api_key="123"  # Any string will work
)

# Send request
response = client.chat.completions.create(
    model="Qwen2.0-7B-SSD",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "How to learn programming?"}
    ],
    stream=True
)

# Process streaming response
for chunk in response:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)
```

### Other Language Clients

Since GenieAPIService is compatible with OpenAI API, you can use any client library that supports OpenAI API:

- **JavaScript/TypeScript**: `openai` npm package
- **Java**: OpenAI Java SDK
- **Go**: go-openai
- **Rust**: async-openai
- **PHP**: openai-php

Simply set the `base_url` to the GenieAPIService address.

---

## API Reference

GenieAPIService provides multiple HTTP API endpoints.

### 1. Chat Completion Endpoint

**Endpoint**: `POST /v1/chat/completions`

**Request Example**:

```json
{
  "model": "Qwen2.0-7B-SSD",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "How to learn programming?"}
  ],
  "stream": true,
  "size": 4096,
  "temp": 1.5,
  "top_k": 13,
  "top_p": 0.6,
  "seed": 146
}
```

**Parameter Descriptions**:

- `model`: Model name (required)
- `messages`: Message list (required)
- `stream`: Whether to stream output (default: false)
- `size`: Maximum output token count (default: 4096)
- `temp`: Temperature parameter (default: 1.0)
- `top_k`: Top-K sampling (default: 40)
- `top_p`: Top-P sampling (default: 0.9)
- `seed`: Random seed (optional)

**Response Example (Non-Streaming)**:

```json
{
  "id": "chatcmpl-123",
  "object": "chat.completion",
  "created": 1677652288,
  "model": "Qwen2.0-7B-SSD",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Suggestions for learning programming..."
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 100,
    "total_tokens": 120
  }
}
```

**Response Example (Streaming)**:

```
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"Qwen2.0-7B-SSD","choices":[{"index":0,"delta":{"content":"Learn"},"finish_reason":null}]}

data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"Qwen2.0-7B-SSD","choices":[{"index":0,"delta":{"content":"ing"},"finish_reason":null}]}

data: [DONE]
```

### 2. Model List Endpoint

**Endpoint**: `GET /v1/models`

**Response Example**:

```json
{
  "object": "list",
  "data": [
    {
      "id": "Qwen2.0-7B-SSD",
      "object": "model",
      "created": 1677652288,
      "owned_by": "system"
    },
    {
      "id": "qwen2.5vl3b",
      "object": "model",
      "created": 1677652288,
      "owned_by": "system"
    }
  ]
}
```

### 3. Text Splitter Endpoint

**Endpoint**: `POST /v1/textsplitter`

**Request Example**:

```json
{
  "text": "This is a very long text...",
  "max_length": 128,
  "separators": ["\n\n", "\n", ".", "!", "?", ",", " ", ""]
}
```

**Parameter Descriptions**:

- `text`: Text to split (required)
- `max_length`: Maximum token count per segment (required)
- `separators`: Separator priority list (required)

**Response Example**:

```json
{
  "content": [
    {
      "text": "First segment text...",
      "length": 120
    },
    {
      "text": "Second segment text...",
      "length": 115
    }
  ]
}
```

### 4. Stop Output Endpoint

**Endpoint**: `POST /stop`

**Request Example**:

```json
{
  "text": "stop"
}
```

**Function**: Immediately stop the current model output.

### 5. Clear History Endpoint

**Endpoint**: `POST /clear`

**Request Example**:

```json
{
  "text": "clear"
}
```

**Function**: Clear conversation history saved on the server.

### 6. Reload History Endpoint

**Endpoint**: `POST /reload`

**Request Example**:

```json
{
  "action": "import_history",
  "history": [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hello! How can I help you?"},
    {"role": "user", "content": "Tell me about Beijing"},
    {"role": "assistant", "content": "Beijing is the capital of China..."}
  ]
}
```

**Function**: Upload conversation history from client to server.

### 7. Fetch History Endpoint

**Endpoint**: `POST /fetch`

**Response Example**:

```json
{
  "history": [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hello! How can I help you?"}
  ]
}
```

### 8. Get Model Context Size Endpoint

**Endpoint**: `POST /contextsize`

**Request Example**:

```json
{
  "modelName": "Qwen2.0-7B-SSD"
}
```

**Response Example**:

```json
{
  "contextsize": 4096
}
```

### 9. Get Model Performance Info Endpoint

**Endpoint**: `GET /profile`

**Response Example**:

```json
{
  "model": "Qwen2.0-7B-SSD",
  "tokens_per_second": 25.5
}
```

### 10. Stop Service Endpoint

**Endpoint**: `POST /servicestop`

**Request Example**:

```json
{
  "text": "stop"
}
```

**Function**: Terminate the GenieAPIService service process.

---

## Python Client Examples

### Basic Chat Example

```python
from openai import OpenAI

# Configure client
BASE_URL = "http://127.0.0.1:8910/v1"
client = OpenAI(base_url=BASE_URL, api_key="123")

# Send message
messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "How to learn programming?"}
]

# Streaming output
response = client.chat.completions.create(
    model="Qwen2.0-7B-SSD",
    stream=True,
    messages=messages
)

for chunk in response:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)
```

### Non-Streaming Output Example

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8910/v1", api_key="123")

response = client.chat.completions.create(
    model="Qwen2.0-7B-SSD",
    messages=[
        {"role": "system", "content": "You are a math teacher."},
        {"role": "user", "content": "What is the Pythagorean theorem?"}
    ]
)

print(response.choices[0].message.content)
```

### Tool Calling Example

```python
from openai import OpenAI
import json

client = OpenAI(base_url="http://127.0.0.1:8910/v1", api_key="123")

# Define tools
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "Get the current weather for a specified city",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name, e.g., Beijing"
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "Temperature unit"
                    }
                },
                "required": ["location", "unit"]
            }
        }
    }
]

# Send request
response = client.chat.completions.create(
    model="Qwen2.0-7B-SSD",
    messages=[
        {"role": "user", "content": "What's the weather like in Beijing today?"}
    ],
    tools=tools,
    tool_choice="auto"
)

# Process tool calls
message = response.choices[0].message
if message.tool_calls:
    for tool_call in message.tool_calls:
        function_name = tool_call.function.name
        function_args = json.loads(tool_call.function.arguments)
        
        # Call actual function
        if function_name == "get_current_weather":
            result = get_current_weather(
                location=function_args["location"],
                unit=function_args["unit"]
            )
            
            # Return result to model
            messages.append(message)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result
            })
            
            # Get final response
            final_response = client.chat.completions.create(
                model="Qwen2.0-7B-SSD",
                messages=messages
            )
            print(final_response.choices[0].message.content)
```

### Text Splitting Example

```python
import requests

url = "http://127.0.0.1:8910/v1/textsplitter"

text = """
This is a very long text content...
It needs to be split based on token count...
"""

separators = ["\n\n", "\n", ".", "!", "?", ",", " ", ""]

body = {
    "text": text,
    "max_length": 128,
    "separators": separators
}

response = requests.post(url, json=body)
result = response.json()

for i, item in enumerate(result["content"], 1):
    print(f"Segment {i}:")
    print(f"Text: {item['text']}")
    print(f"Token count: {item['length']}")
    print()
```

### History Management Example

```python
import requests

BASE_URL = "http://127.0.0.1:8910"

# Clear history
def clear_history():
    url = f"{BASE_URL}/clear"
    response = requests.post(url, json={"text": "clear"})
    return response.status_code == 200

# Reload history
def reload_history(history_list):
    url = f"{BASE_URL}/reload"
    history_data = {
        "action": "import_history",
        "history": history_list
    }
    response = requests.post(url, json=history_data)
    return response.status_code == 200

# Fetch history
def fetch_history():
    url = f"{BASE_URL}/fetch"
    response = requests.post(url)
    return response.json()

# Usage example
if __name__ == "__main__":
    # Clear history
    clear_history()
    
    # Load new history
    history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hello! How can I help you?"},
        {"role": "user", "content": "Tell me about Beijing"},
        {"role": "assistant", "content": "Beijing is the capital of China..."}
    ]
    reload_history(history)
    
    # Get current history
    current_history = fetch_history()
    print(current_history)
```

### Vision Language Model Example

```python
from openai import OpenAI
import base64

client = OpenAI(base_url="http://127.0.0.1:8910/v1", api_key="123")

# Read and encode image
def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

# Encode image
image_base64 = encode_image("test.png")

# Build message
messages = [
    {
        "role": "user",
        "content": {
            "question": "What does this image describe?",
            "image": image_base64
        }
    }
]

# Send request
response = client.chat.completions.create(
    model="qwen2.5vl3b",
    messages=messages,
    stream=True
)

# Process response
for chunk in response:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)
```

---

## FAQ

### 1. Service Startup Failure

**Problem**: Running `GenieAPIService.exe` prompts that DLL files cannot be found.

**Solution**:
- Ensure Qualcomm® AI Runtime SDK runtime libraries exist in the current path
- Install Visual C++ Redistributable

### 2. Model Loading Failure

**Problem**: After service startup, it prompts "Failed to load model".

**Solution**:
- Check if the configuration file path is correct
- Confirm model files are complete and not corrupted
- Check if model directory structure meets requirements
- View log files for detailed error information

### 3. Port Already in Use

**Problem**: When starting service, it prompts "service already exist".

**Solution**:
- Check if a GenieAPIService instance is already running
- Use the `-p` parameter to specify a different port
- Windows: Use `netstat -ano | findstr 8910` to find the process occupying the port
- Terminate the process occupying the port or restart the system

### 4. Input Length Exceeded

**Problem**: When sending request, it prompts input length exceeds limit.

**Solution**:
- Use text splitter endpoint to segment long text
- Increase `--min_output_num` parameter value to reduce output reserved space
- Use models that support larger context
- Formula: Input length + history length + minimum output length ≤ model context size

### 5. Android Service Killed

**Problem**: Service automatically stops after running for a while on Android device.

**Solution**:
- Go to Settings → Battery → Power saving settings → App battery management
- Find GenieAPI application
- Select "Allow background activity"
- Disable battery optimization

### 6. Slow Response Speed

**Problem**: Model inference speed is very slow.

**Solution**:
- Confirm device supports NPU and is correctly configured
- Check if correct model format is used (BIN format has best performance)
- Reduce `size` parameter value to limit output length
- Close unnecessary background applications to free up resources
- Use smaller models (e.g., 3B instead of 7B)

### 7. Tool Calling Not Working

**Problem**: Model does not call defined tools.

**Solution**:
- Confirm the model being used supports tool calling functionality
- Disable thinking mode (do not use `-t` parameter)
- Check if tool definition format is correct
- Use `-a` parameter to output all text for debugging

### 8. History Not Working

**Problem**: After enabling history, model still doesn't remember previous conversations.

**Solution**:
- Confirm `-n` parameter was used at startup
- Check if history was cleared
- Use `/fetch` endpoint to view current history
- Ensure history format is correct

### 9. Vision Language Model Cannot Recognize Images

**Problem**: VLM model returns errors or cannot understand image content.

**Solution**:
- Confirm image is correctly Base64 encoded
- Check if image format is supported (PNG, JPEG recommended)
- Confirm model files are complete, especially `veg.serialized.bin`
- Check if image size is reasonable (recommended not to exceed 2MB)

### 10. Out of Memory

**Problem**: Runtime prompts out of memory or system becomes slow.

**Solution**:
- Use smaller models
- Reduce `--num_response` parameter value
- Close other memory-consuming applications
- Increase device physical memory
- Use models with higher quantization

---

## Technical Support

### Official Resources

- **GitHub Repository**: https://github.com/quic/ai-engine-direct-helper
- **Issue Reporting**: https://github.com/quic/ai-engine-direct-helper/issues
- **Release Page**: https://github.com/quic/ai-engine-direct-helper/releases

### Documentation Resources

- **API Documentation**: [docs/API.md](../samples/genie/c++/docs/API.md)
- **Build Documentation**: [docs/BUILD.md](../samples/genie/c++/docs/BUILD.md)
- **Usage Documentation**: [docs/USAGE.MD](../samples/genie/c++/docs/USAGE.MD)
- **VLM Deployment Documentation**: [docs/VLM_DEPLOYMENT.MD](../samples/genie/c++/docs/VLM_DEPLOYMENT.MD)

### Community Support

- Search for similar issues in GitHub Issues
- View example code: `samples/genie/python/`, `samples/genie/c%2B%2B/Service/examples/GenieAPIClient` directories
- Reference test code: `samples/genie/c%2B%2B/Service/test/genietest.py`

### Contact

For technical support, please:
1. Check the FAQ section of this manual
2. Search GitHub Issues
3. Create a new Issue on GitHub with detailed error information and logs

---

## Disclaimer

This software is provided "as is" without any express or implied warranties. The authors and contributors are not liable for any damages arising from the use of this software. The code may be incomplete or insufficiently tested. Users must evaluate its suitability and assume all related risks.

**Note**: Code contributions are welcome. Ensure thorough testing before deploying in critical systems.

---

## Version Information

- **GenieAPIService Version**: 2.1.3
- **QAIRT SDK Version**: 2.42.0
- **Documentation Version**: 1.0
- **Last Updated**: 2026-01-28

---

## Appendix

### A. Supported Model List

- Qwen2.0-7B-SSD
- Qwen2.5-VL-3B
- Llama 3
- IBM-Granite-v3.1-8B
- Phi-4 Multimodal
- Llama series (requires GGUF format support)
- Other compatible models

### B. Performance Optimization Recommendations

1. **Use NPU Acceleration**: Ensure using QNN format models
2. **Set Context Length Reasonably**: Do not exceed the model's maximum supported value
3. **Optimize Batch Processing**: Consider batch processing for multiple requests
4. **Cache Management**: Use history functionality reasonably
5. **Resource Monitoring**: Regularly check memory and CPU usage

### C. Security Recommendations

1. **Network Security**: Do not expose service directly to the public internet
2. **Access Control**: Add authentication in production environments
3. **Data Privacy**: Process sensitive data locally, do not upload to cloud
4. **Regular Updates**: Update to the latest version promptly for security fixes

### D. Development Recommendations

1. **Error Handling**: Clients should implement comprehensive error handling mechanisms
2. **Timeout Settings**: Set reasonable request timeout periods
3. **Retry Mechanism**: Implement exponential backoff retry strategy
4. **Logging**: Record key operations and error information
5. **Testing**: Conduct thorough functional and performance testing before deployment

---

**Thank you for using GenieAPIService!**

If you have any questions or suggestions, please visit our GitHub repository.
