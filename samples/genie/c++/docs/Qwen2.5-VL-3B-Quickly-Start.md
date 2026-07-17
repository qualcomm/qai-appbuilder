# Qwen2.5-VL-3B Model On-Device Deployment

## Part 1: Using on Windows Platform

This section explains how to configure and run the Qwen2.5-VL-3B model in a Windows environment.

### 1.1 Resource Download and Preparation

- **Download model files**:<br>
  Download the model matching your platform from [Qwen2.5-VL-3B](https://www.aidevhome.com/?id=51)
  and place it in the `qai-appbuilder\samples\genie\python\models` directory.

- **Download the Genie Service Program**:<br>
  Go to the GitHub Releases page and download the `GenieAPIService_<version>_QAIRT_<qairt_version>_<stub>.zip` package that
  matches your NPU architecture (the exact version numbers change with every release, so this guide intentionally
  does not hard-code one — check the [Releases](https://github.com/qualcomm/qai-appbuilder/releases) page for the
  latest build).

- **Extract the file**:<br>
  Unzip the downloaded compressed package into the project code directory `qai-appbuilder\samples`.

### 1.2 Starting Services and Running Examples

Open the terminal, navigate to the samples directory, and run the service and client commands
separately.

```
# 1. Entry the directory
cd qai-appbuilder\samples

# 2. Start the GenieAPI Service (loading the config file)
GenieAPIService\GenieAPIService.exe -c "genie\python\models\qwen2.5vl3b\config.json" -l
 [W] load successfully! use second: 4.56947
 [W] Model load successfully: qwen2.5vl3b
 [W] GenieService::setupHttpServer start
 [W] GenieService::setupHttpServer end
 [A] [OK] Genie API Service IS Running.
 [A] [OK] Genie API Service -> http://0.0.0.0:8910

# 3. running the client for test(ensure test.png is existed in the current directory)
GenieAPIClient.exe --prompt "what is the image descript?" --img test.png --stream --model qwen2.5vl3b
```

## Part 2: Using on Android Platform

### 2.1 Resource Download and Preparation

- **Download model files**:<br>
  As with Windows, download the model matching your platform from
  [Qwen2.5-VL-3B](https://www.aidevhome.com/?id=51) and place it in the `/sdcard/GenieModels/` directory.


- **Download and Install APK**: <br>
  Download the latest `GenieAPIService.apk` from [GitHub Releases](https://github.com/qualcomm/qai-appbuilder/releases)
  and install it on the device.

### 2.2 Example Application Compilation and Execution

The Android sample app source is located in the project directory; build it yourself.

- **Source path**:
  `samples\android\GenieChat`


- **Instructions**:<br>
  Open this directory in Android Studio, build it, install it on the device, and run it alongside the
  installed `GenieAPIService`.

### 2.3 Example Application Screenshots

Example in **Geniechat**

![img.png](img/6.png)

![img.png](img/5.png)

## Part 3: Python Calling Guide

Whether GenieAPIService.exe runs on Windows or GenieAPIService.apk runs on Android, the service displays an IP
address and port once it starts (e.g., 127.0.0.1:8910, or the phone's IP). Call this service from Python via the
OpenAI-compatible interface.

### 3.1 Resource Download and Preparation

Install the `openai` library:

```pip install openai```

### 3.2 Python Calling Code (vl_client.py)

Create a Python script (e.g., vl_client.py), copy the following code into it, and update the IP address for your
environment.

```
import argparse
import base64
import requests # Added: used to download images from the web
import os
from openai import OpenAI

IP_ADDR = "127.0.0.1:8910"

parser = argparse.ArgumentParser()
parser.add_argument("--stream", action="store_true")
parser.add_argument("--prompt", default="Describe this image", type=str)
parser.add_argument("--image", required=True, type=str, help="Path to local image file or Image URL")
args = parser.parse_args()

# 1. Modified helper function: supports local paths and URLs
def encode_image(image_input):
    # Determine whether it's a URL
    if image_input.startswith(('http://', 'https://')):
        try:
            print(f"Downloading image from URL: {image_input}...")
            response = requests.get(image_input, timeout=10)
            response.raise_for_status() # Check that the request succeeded
            # Directly convert the downloaded binary content to Base64
            return base64.b64encode(response.content).decode('utf-8')
        except Exception as e:
            raise Exception(f"Failed to download image from URL: {e}")
   
    # Otherwise treat it as a local file
    else:
        try:
            if not os.path.exists(image_input):
                raise FileNotFoundError(f"Local file not found: {image_input}")
           
            with open(image_input, "rb") as image_file:
                return base64.b64encode(image_file.read()).decode('utf-8')
        except Exception as e:
            raise Exception(f"Failed to load local image: {e}")

# Obtain the Base64-encoded image
try:
    base64_image = encode_image(args.image)
except Exception as e:
    print(f"Error: {e}")
    exit(1)

client = OpenAI(base_url="http://" + IP_ADDR + "/v1", api_key="123")

# Construct the special message structure required by the Genie Service (VL model)
custom_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {
        "role": "user",
        "content": {
            "question": args.prompt,  
            "image": base64_image     # Regardless of whether the source is a URL or local, send Base64 to the server here
        }
    }
]

extra_body = {
    "size": 4096,
    "temp": 1.5,
    "top_k": 13,
    "top_p": 0.6,
    "messages": custom_messages
}

model_name = "qwen2.5vl3b-8380-2.42"  # replace with the actual model name you deployed (the "name" it was
                                       # loaded/registered under); this exact string is only an example and is
                                       # not guaranteed to match a model directory in your own deployment.
placeholder_msgs = [{"role": "user", "content": "placeholder"}]

# Send the request
try:
    if args.stream:
        response = client.chat.completions.create(
            model=model_name,
            stream=True,
            messages=placeholder_msgs,
            extra_body=extra_body
        )

        for chunk in response:
            if chunk.choices:
                content = chunk.choices[0].delta.content
                if content is not None:
                    print(content, end="", flush=True)
        print() # newline
    else:
        response = client.chat.completions.create(
            model=model_name,
            messages=placeholder_msgs,
            extra_body=extra_body
        )
        if response.choices:
            print(response.choices[0].message.content)

except Exception as e:
    print(f"\nRequest failed: {e}")

```

### 3.3 Running Script

Run the script in the command line, specifying the image path and (optional) prompt:

```
python vl_client.py --image test.png --prompt "what is image descript?" --stream
python vl_client.py --image "https://www.google.com/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png" --prompt "What implies in this logo?"
```

### 3.4 Two supported message formats

The script above uses a flat, non-standard `content` object — `{"question": ..., "image": base64_image}` — which
the server still supports (parsed via `ModelInputBuilder::ProcessObject`). This is a GenieAPIClient-style
shorthand kept mainly for backward compatibility.

The server also supports (and, for new integrations, recommends) the **standard OpenAI `content` array format**
(parsed via `ModelInputBuilder::ProcessArray`), which has better compatibility with off-the-shelf OpenAI client
libraries and tooling:

```python
custom_messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {
        "role": "user",
        "content": [
            {"type": "text", "text": args.prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64_image}}
        ]
    }
]
```

Both formats are accepted side by side by the server — pick whichever is more convenient for your integration.
See the "Chat Completions" section in [API.md](API.md) for the full format reference, including audio
(`input_audio`) support.
